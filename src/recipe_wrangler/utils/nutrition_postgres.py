# Purpose: Postgres nutrition fetch helpers using SQLAlchemy with docker psql fallback.

import json
import logging
import os
import subprocess
import threading
from typing import Optional
from contextlib import contextmanager

from sqlalchemy import create_engine, text, Engine
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy.exc import SQLAlchemyError
from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

logger = logging.getLogger(__name__)


def _env(name: str, fallback: str) -> str:
    return os.getenv(name) or fallback


def _get_config():
    return {
        "db_name": _env("NUTRITION_DB", _env("POSTGRES_DB", "nutrients")),
        "db_user": _env("NUTRITION_USER", _env("POSTGRES_USER", "postgres")),
        "db_password": _env("NUTRITION_PASSWORD", _env("POSTGRES_PASSWORD", "postgres")),
        "db_host": _env("NUTRITION_HOST", _env("POSTGRES_HOST", "localhost")),
        "db_port": _env("NUTRITION_PORT", _env("POSTGRES_PORT", "5432")),
        "schema": _env("NUTRITION_SCHEMA", "public"),
        "ingredients_table": _env("NUTRITION_INGREDIENTS_TABLE", "nutrients-ingredients-usda"),
        "irish_ingredients_table": _env("NUTRITION_INGREDIENTS_IRISH_TABLE", "nutrients-ingredients-irish"),
        "hungarian_ingredients_table": _env(
            "NUTRITION_INGREDIENTS_HUNGARIAN_TABLE",
            "nutrients-ingredients-hungarian",
        ),
        "eu_ingredients_table": _env(
            "NUTRITION_INGREDIENTS_EU_TABLE",
            "nutrients-ingredients-eu",
        ),
        "profiles_table": _env("NUTRITION_PROFILES_TABLE", "nutrients-recipe-profiles"),
        "use_docker": _env("NUTRITION_USE_DOCKER", "0") == "1",
        "container": _env("NUTRITION_CONTAINER", _env("POSTGRES_CONTAINER", "")),
    }


def _run_psql_fallback(query: str, cfg: dict[str, str | bool]) -> str:
    env = os.environ.copy()
    env["PGPASSWORD"] = str(cfg["db_password"])

    if cfg["use_docker"] and cfg["container"]:
        cmd = [
            "docker",
            "exec",
            "-i",
            str(cfg["container"]),
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-U",
            str(cfg["db_user"]),
            "-d",
            str(cfg["db_name"]),
            "-c",
            query,
        ]
    else:
        cmd = [
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-h",
            str(cfg["db_host"]),
            "-p",
            str(cfg["db_port"]),
            "-U",
            str(cfg["db_user"]),
            "-d",
            str(cfg["db_name"]),
            "-c",
            query,
        ]

    result = subprocess.run(
        cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or "psql fallback failed"
        raise RuntimeError(msg)
    return result.stdout.strip()


# Global engine instance (lazy-initialized)
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """Get or create the SQLAlchemy engine with connection pooling."""
    global _engine
    if _engine is None:
        cfg = _get_config()

        # Build connection URL
        # Format: postgresql://username:password@host:port/database
        connection_url = (
            f"postgresql://{cfg['db_user']}:{cfg['db_password']}"
            f"@{cfg['db_host']}:{cfg['db_port']}/{cfg['db_name']}"
        )

        # Create engine with connection pooling
        # pool_pre_ping=True: Test connections before using them (handles stale connections)
        _engine = create_engine(
            connection_url,
            poolclass=QueuePool,
            pool_size=int(_env("NUTRITION_POOL_SIZE", _env("POSTGRES_POOL_SIZE", "5"))),
            max_overflow=int(_env("NUTRITION_MAX_OVERFLOW", _env("POSTGRES_MAX_OVERFLOW", "10"))),
            pool_pre_ping=True,
            echo=False,  # Set to True to see SQL queries in logs
        )

    return _engine


@contextmanager
def get_connection():
    """Context manager for database connections."""
    engine = get_engine()
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


def fetch_ingredient_nutrition_by_usda_id(usda_id: str) -> Optional[dict]:
    """
    Return ingredient nutrient record from Postgres by USDA id.

    Args:
        usda_id: USDA food identifier

    Returns:
        Dictionary with keys: usda_id, food_name, nutrients
        None if not found

    Raises:
        SQLAlchemyError: If database query fails
    """
    cfg = _get_config()

    # Build the query using SQLAlchemy text() with bound parameters
    # This prevents SQL injection by properly escaping parameters
    query = text("""
        SELECT row_to_json(t) as data
        FROM (
            SELECT usda_id, food_name, nutrients
            FROM :schema.:table
            WHERE usda_id = :usda_id
            LIMIT 1
        ) t
    """).bindparams(
        schema=cfg["schema"],
        table=cfg["ingredients_table"],
        usda_id=str(usda_id)
    )

    # Note: SQLAlchemy doesn't support binding schema/table names directly
    # We need to use string formatting for identifiers (but still safe with our config)
    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT usda_id, food_name, nutrients
            FROM "{cfg['schema']}"."{cfg['ingredients_table']}"
            WHERE usda_id = :usda_id
            LIMIT 1
        ) t
    """

    try:
        with get_connection() as conn:
            result = conn.execute(text(query_str), {"usda_id": str(usda_id)})
            row = result.fetchone()

            if row is None:
                return None

            # row[0] or row.data contains the JSON object
            return row[0]

    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            query = f"""
                SELECT row_to_json(t)
                FROM (
                    SELECT usda_id, food_name, nutrients
                    FROM "{cfg['schema']}"."{cfg['ingredients_table']}"
                    WHERE usda_id = '{str(usda_id).replace("'", "''")}'
                    LIMIT 1
                ) t
            """
            out = _run_psql_fallback(query, cfg)
            if not out:
                return None
            return json.loads(out)
        raise RuntimeError(f"Failed to fetch ingredient nutrition: {e}") from e


def fetch_recipe_nutrition_by_id(
    recipe_id: str,
    nutrition_source: Optional[str] = None,
) -> Optional[dict]:
    """
    Return recipe nutrition record from Postgres by recipe id.

    Args:
        recipe_id: Recipe identifier

    Returns:
        Dictionary with keys: recipe_id, title, total_nutrients,
        total_nutrients_per_serving, nutri_score, source
        None if not found

    Raises:
        SQLAlchemyError: If database query fails
    """
    cfg = _get_config()

    # Build query with proper identifier quoting for schema/table
    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source, nutrition_source
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = :recipe_id
            {"AND nutrition_source = :nutrition_source" if nutrition_source else ""}
            LIMIT 1
        ) t
    """

    try:
        with get_connection() as conn:
            params = {"recipe_id": str(recipe_id)}
            if nutrition_source:
                params["nutrition_source"] = str(nutrition_source)
            result = conn.execute(text(query_str), params)
            row = result.fetchone()

            if row is None:
                return None

            return row[0]

    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            query = f"""
                SELECT row_to_json(t)
                FROM (
                    SELECT recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source, nutrition_source
                    FROM "{cfg['schema']}"."{cfg['profiles_table']}"
                    WHERE recipe_id = '{str(recipe_id).replace("'", "''")}'
                    {"AND nutrition_source = '" + str(nutrition_source).replace("'", "''") + "'" if nutrition_source else ""}
                    LIMIT 1
                ) t
            """
            out = _run_psql_fallback(query, cfg)
            if not out:
                return None
            return json.loads(out)
        raise RuntimeError(f"Failed to fetch recipe nutrition: {e}") from e


def fetch_recipe_nutrition_batch(recipe_ids: list[str]) -> dict[str, dict]:
    """
    Return recipe nutrition records from Postgres for many recipe ids at once.

    Args:
        recipe_ids: Recipe identifiers

    Returns:
        {recipe_id: record} where record has the same keys as
        fetch_recipe_nutrition_by_id. Ids with no profile row are absent.
        One record per recipe id (first row wins, matching the single-id
        fetch's LIMIT 1 semantics).

    Raises:
        RuntimeError: If database query fails and no fallback is available
    """
    ids = [str(rid) for rid in recipe_ids if rid]
    if not ids:
        return {}

    cfg = _get_config()

    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT DISTINCT ON (recipe_id)
                recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source, nutrition_source
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = ANY(:recipe_ids)
        ) t
    """

    try:
        with get_connection() as conn:
            result = conn.execute(text(query_str), {"recipe_ids": ids})
            return {str(row[0]["recipe_id"]): row[0] for row in result}

    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            quoted = ", ".join("'" + rid.replace("'", "''") + "'" for rid in ids)
            query = f"""
                SELECT json_agg(row_to_json(t))
                FROM (
                    SELECT DISTINCT ON (recipe_id)
                        recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source, nutrition_source
                    FROM "{cfg['schema']}"."{cfg['profiles_table']}"
                    WHERE recipe_id IN ({quoted})
                ) t
            """
            out = _run_psql_fallback(query, cfg)
            if not out or out == "null":
                return {}
            return {str(rec["recipe_id"]): rec for rec in json.loads(out)}
        raise RuntimeError(f"Failed to fetch recipe nutrition batch: {e}") from e


_REGION_BY_SOURCE = {"usda": "us", "irish": "ie", "hungarian": "hu", "eu": "eu"}


def fetch_ingredient_nutrition_by_eu_id(eu_id: str) -> Optional[dict]:
    """Return ingredient nutrient record from the EU composite Postgres table.

    The row's JSONB ``nutrients`` column is keyed by USDA-canonical nutrient
    names (Protein, Carbohydrate by difference, Total lipid (fat), …), so the
    downstream USDA-shaped consumer in ``nutritional_calculator`` reads it
    directly without a per-source branch.
    """
    cfg = _get_config()
    query_str = f"""
        SELECT row_to_json(t) AS data
        FROM (
            SELECT id, food_name, source, country, food_group, nutrients
            FROM "{cfg['schema']}"."{cfg['eu_ingredients_table']}"
            WHERE id = :eu_id
            LIMIT 1
        ) t
    """
    try:
        with get_connection() as conn:
            row = conn.execute(text(query_str), {"eu_id": str(eu_id)}).fetchone()
            if row is None:
                return None
            return row[0]
    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            q = f"""
                SELECT row_to_json(t)
                FROM (
                    SELECT id, food_name, source, country, food_group, nutrients
                    FROM "{cfg['schema']}"."{cfg['eu_ingredients_table']}"
                    WHERE id = '{str(eu_id).replace("'", "''")}'
                    LIMIT 1
                ) t
            """
            out = _run_psql_fallback(q, cfg)
            if not out:
                return None
            return json.loads(out)
        raise RuntimeError(f"Failed to fetch EU ingredient nutrition: {e}") from e


def fetch_all_recipe_scores() -> dict[str, dict]:
    """Return per-region nutri scores + sustainability for every profiled recipe.

    Shape: {recipe_id: {"us": {nutri_score, nutri_color}, "ie": {...}, "hu": {...},
                        "sust_score": float|None}}

    Nutri score is region-dependent — the same recipe scored against the US,
    Irish or Hungarian food-composition DB can grade differently. Sustainability
    is region-independent, so the first non-null value is used. One bulk query.
    """
    cfg = _get_config()
    query_str = f"""
        SELECT recipe_id, nutrition_source, nutri_score, total_sustainability_per_serving
        FROM "{cfg['schema']}"."{cfg['profiles_table']}"
        WHERE nutrition_source IN ('usda', 'irish', 'hungarian', 'eu')
    """
    scores: dict[str, dict] = {}
    with get_connection() as conn:
        for recipe_id, source, nutri_score, sust in conn.execute(text(query_str)):
            region = _REGION_BY_SOURCE.get(source)
            if recipe_id is None or region is None:
                continue
            entry = scores.setdefault(str(recipe_id), {"sust_score": None})
            grade = color = None
            if isinstance(nutri_score, dict):
                grade = nutri_score.get("nutri_score")
                color = nutri_score.get("color")
            entry[region] = {"nutri_score": grade, "nutri_color": color}
            if entry["sust_score"] is None and sust is not None:
                entry["sust_score"] = float(sust)
    return scores


def fetch_recipe_region_scores(recipe_id: str) -> dict:
    """Single-recipe variant of fetch_all_recipe_scores — same shape, one ID.

    Used by the runtime recipes_v2 projection on create/update, where loading
    the full corpus score map per request is not an option.
    """
    cfg = _get_config()
    query_str = f"""
        SELECT nutrition_source, nutri_score, total_sustainability_per_serving
        FROM "{cfg['schema']}"."{cfg['profiles_table']}"
        WHERE recipe_id = :recipe_id
          AND nutrition_source IN ('usda', 'irish', 'hungarian', 'eu')
    """
    entry: dict = {"sust_score": None}
    with get_connection() as conn:
        rows = conn.execute(text(query_str), {"recipe_id": str(recipe_id)})
        for source, nutri_score, sust in rows:
            region = _REGION_BY_SOURCE.get(source)
            if region is None:
                continue
            grade = color = None
            if isinstance(nutri_score, dict):
                grade = nutri_score.get("nutri_score")
                color = nutri_score.get("color")
            entry[region] = {"nutri_score": grade, "nutri_color": color}
            if entry["sust_score"] is None and sust is not None:
                entry["sust_score"] = float(sust)
    return entry


# The profiles table DDL (create/alter/index) used to run on EVERY trace
# write — three extra round-trips plus lock traffic per write. It only needs
# to run once per process; the probe also remembers whether the
# (recipe_id, nutrition_source) unique index exists so each write picks
# ON CONFLICT vs update-then-insert without re-probing.
_PROFILES_TABLE_READY = False
_PROFILES_HAS_UNIQUE = False
_PROFILES_TABLE_LOCK = threading.Lock()


def _ensure_profiles_table_ready(
    create_table_sql: str,
    alter_table_sql: str,
    ensure_unique_index_sql: str,
) -> None:
    global _PROFILES_TABLE_READY, _PROFILES_HAS_UNIQUE
    if _PROFILES_TABLE_READY:
        return
    with _PROFILES_TABLE_LOCK:
        if _PROFILES_TABLE_READY:
            return
        with get_connection() as conn:
            tx = conn.begin()
            try:
                conn.execute(text(create_table_sql))
                conn.execute(text(alter_table_sql))
                tx.commit()
            except Exception:
                tx.rollback()
                raise
            tx = conn.begin()
            try:
                conn.execute(text(ensure_unique_index_sql))
                tx.commit()
                has_unique = True
            except SQLAlchemyError:
                tx.rollback()
                has_unique = False
                logger.warning(
                    "profiles table lacks the (recipe_id, nutrition_source) unique "
                    "index and it could not be created (duplicate rows?); trace "
                    "writes will use update-then-insert for this process"
                )
        _PROFILES_HAS_UNIQUE = has_unique
        _PROFILES_TABLE_READY = True


# Columns consumed by readers. The archival `trace` column (tens of KB per
# row, written for offline debugging) is deliberately absent: no runtime
# consumer reads it, so fetching it only inflates every trace read.
_TRACE_READ_COLUMNS = """
                recipe_id,
                title,
                source,
                nutrition_source,
                total_nutrients,
                total_nutrients_per_serving,
                nutri_score,
                nutri_score_breakdown,
                nutrition_profiling_details,
                nutrition_profiling_debug,
                total_sustainability,
                total_sustainability_per_serving,
                sustainability_per_kg,
                sustainability_profiling_details,
                pipeline_version,
                computed_at,
                updated_at
"""


def fetch_recipe_profiling_traces_by_id(recipe_id: str) -> list[dict]:
    """Return every region's profiling trace row for a recipe in ONE query.

    Freshest first. Lets callers that need multiple views of the profile
    (preferred region, any-region fallback, ground-truth sources) do the
    selection in Python instead of issuing one query per view.
    """
    cfg = _get_config()
    query_str = f"""
            SELECT {_TRACE_READ_COLUMNS}
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = :recipe_id
            ORDER BY updated_at DESC NULLS LAST
    """
    try:
        with get_connection() as conn:
            result = conn.execute(text(query_str), {"recipe_id": str(recipe_id)})
            return [dict(row._mapping) for row in result.fetchall()]
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to fetch recipe profiling traces: {e}") from e


def fetch_recipe_profiling_trace_by_id(
    recipe_id: str,
    nutrition_source: Optional[str] = None,
) -> Optional[dict]:
    """
    Return recipe profiling trace from Postgres by recipe id.

    Returns the row as dict from the profiles table (without the archival
    `trace` column), or None if not found.
    """
    cfg = _get_config()
    query_str = f"""
            SELECT {_TRACE_READ_COLUMNS}
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = :recipe_id
            {"AND nutrition_source = :nutrition_source" if nutrition_source else ""}
            LIMIT 1
    """
    try:
        with get_connection() as conn:
            params = {"recipe_id": str(recipe_id)}
            if nutrition_source:
                params["nutrition_source"] = str(nutrition_source)
            result = conn.execute(text(query_str), params)
            row = result.fetchone()
            if row is None:
                return None
            
            # Check if we got a single column containing JSON (fallback or row_to_json)
            if len(row) == 1:
                if isinstance(row[0], dict):
                    return row[0]
                if isinstance(row[0], str):
                    return json.loads(row[0])
            
            # Map the row to a dict using SQLAlchemy mapping
            return dict(row._mapping)
    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            query = f"""
                SELECT row_to_json(t)
                FROM (
                    SELECT
                        recipe_id,
                        title,
                        source,
                        nutrition_source,
                        total_nutrients,
                        total_nutrients_per_serving,
                        nutri_score,
                        nutri_score_breakdown,
                        nutrition_profiling_details,
                        nutrition_profiling_debug,
                        total_sustainability,
                        total_sustainability_per_serving,
                        sustainability_per_kg,
                        sustainability_profiling_details,
                        trace,
                        pipeline_version,
                        computed_at,
                        updated_at
                    FROM "{cfg['schema']}"."{cfg['profiles_table']}"
                    WHERE recipe_id = '{str(recipe_id).replace("'", "''")}'
                    {"AND nutrition_source = '" + str(nutrition_source).replace("'", "''") + "'" if nutrition_source else ""}
                    LIMIT 1
                ) t
            """
            out = _run_psql_fallback(query, cfg)
            if not out:
                return None
            return json.loads(out)
        raise RuntimeError(f"Failed to fetch recipe profiling trace: {e}") from e


def upsert_recipe_profiling_trace(record: dict) -> None:
    """
    Upsert a recipe profiling trace row into Postgres.

    Expected keys:
      recipe_id (required), title, source, nutrition_source,
      total_nutrients, total_nutrients_per_serving, nutri_score, nutri_score_breakdown,
      nutrition_profiling_details, nutrition_profiling_debug, trace,
      pipeline_version,
      computed_at (optional, timestamptz-compatible string)
    """
    recipe_id = str(record.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError("upsert_recipe_profiling_trace requires recipe_id")

    cfg = _get_config()
    source = record.get("source")
    source_text = str(source or "").strip()
    raw_source_id = record.get("source_id")
    if source_text == "recipe1m":
        resolved_source_id = "urn:rcollection:recipe1m"
    elif source_text == "HealthyFoods":
        resolved_source_id = "urn:rcollection:healthyfood"
    elif source_text == "FoodHero":
        resolved_source_id = "urn:rcollection:foodhero"
    elif source_text == "Curated Irish Recipes":
        resolved_source_id = "urn:rcollection:rcsi-recipes"
    elif source_text == "MyPlate":
        resolved_source_id = "urn:rcollection:myplate"
    elif raw_source_id is not None and str(raw_source_id).strip():
        resolved_source_id = str(raw_source_id).strip()
    else:
        resolved_source_id = source_text or None

    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS "{cfg['schema']}"."{cfg['profiles_table']}" (
            recipe_id text NOT NULL,
            nutrition_source text NOT NULL,
            title text,
            source text,
            source_id text,
            total_nutrients jsonb,
            total_nutrients_per_serving jsonb,
            nutri_score jsonb,
            nutri_score_breakdown jsonb,
            nutrition_profiling_details jsonb,
            nutrition_profiling_debug jsonb,
            total_sustainability float8,
            total_sustainability_per_serving float8,
            sustainability_per_kg float8,
            sustainability_profiling_details jsonb,
            trace jsonb,
            pipeline_version text,
            computed_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            PRIMARY KEY (recipe_id, nutrition_source)
        )
    """
    upsert_sql = f"""
        INSERT INTO "{cfg['schema']}"."{cfg['profiles_table']}" (
            recipe_id,
            title,
            source,
            source_id,
            nutrition_source,
            total_nutrients,
            total_nutrients_per_serving,
            nutri_score,
            nutri_score_breakdown,
            nutrition_profiling_details,
            nutrition_profiling_debug,
            total_sustainability,
            total_sustainability_per_serving,
            sustainability_per_kg,
            sustainability_profiling_details,
            trace,
            pipeline_version,
            computed_at,
            updated_at
        )
        VALUES (
            :recipe_id,
            :title,
            :source,
            :source_id,
            :nutrition_source,
            CAST(:total_nutrients AS jsonb),
            CAST(:total_nutrients_per_serving AS jsonb),
            CAST(:nutri_score AS jsonb),
            CAST(:nutri_score_breakdown AS jsonb),
            CAST(:nutrition_profiling_details AS jsonb),
            CAST(:nutrition_profiling_debug AS jsonb),
            :total_sustainability,
            :total_sustainability_per_serving,
            :sustainability_per_kg,
            CAST(:sustainability_profiling_details AS jsonb),
            CAST(:trace AS jsonb),
            :pipeline_version,
            COALESCE(CAST(:computed_at AS timestamptz), now()),
            now()
        )
        ON CONFLICT (recipe_id, nutrition_source) DO UPDATE SET
            title = EXCLUDED.title,
            source = EXCLUDED.source,
            source_id = EXCLUDED.source_id,
            total_nutrients = EXCLUDED.total_nutrients,
            total_nutrients_per_serving = EXCLUDED.total_nutrients_per_serving,
            nutri_score = EXCLUDED.nutri_score,
            nutri_score_breakdown = EXCLUDED.nutri_score_breakdown,
            nutrition_profiling_details = EXCLUDED.nutrition_profiling_details,
            nutrition_profiling_debug = EXCLUDED.nutrition_profiling_debug,
            total_sustainability = EXCLUDED.total_sustainability,
            total_sustainability_per_serving = EXCLUDED.total_sustainability_per_serving,
            sustainability_per_kg = EXCLUDED.sustainability_per_kg,
            sustainability_profiling_details = EXCLUDED.sustainability_profiling_details,
            trace = EXCLUDED.trace,
            pipeline_version = EXCLUDED.pipeline_version,
            computed_at = EXCLUDED.computed_at,
            updated_at = now()
    """
    alter_table_sql = f"""
        ALTER TABLE "{cfg['schema']}"."{cfg['profiles_table']}"
        ADD COLUMN IF NOT EXISTS source_id text,
        ADD COLUMN IF NOT EXISTS total_sustainability float8,
        ADD COLUMN IF NOT EXISTS total_sustainability_per_serving float8,
        ADD COLUMN IF NOT EXISTS sustainability_per_kg float8,
        ADD COLUMN IF NOT EXISTS sustainability_profiling_details jsonb
    """
    # Deployed tables can predate the (recipe_id, nutrition_source) primary
    # key in create_table_sql; without a matching unique constraint the
    # ON CONFLICT upsert raises InvalidColumnReference. Self-heal by creating
    # the unique index (no-op when present), and if that is impossible (e.g.
    # pre-existing duplicate rows) fall back to update-then-insert.
    ensure_unique_index_sql = f"""
        CREATE UNIQUE INDEX IF NOT EXISTS "{cfg['profiles_table']}_recipe_source_key"
        ON "{cfg['schema']}"."{cfg['profiles_table']}" (recipe_id, nutrition_source)
    """
    update_sql = f"""
        UPDATE "{cfg['schema']}"."{cfg['profiles_table']}" SET
            title = :title,
            source = :source,
            source_id = :source_id,
            total_nutrients = CAST(:total_nutrients AS jsonb),
            total_nutrients_per_serving = CAST(:total_nutrients_per_serving AS jsonb),
            nutri_score = CAST(:nutri_score AS jsonb),
            nutri_score_breakdown = CAST(:nutri_score_breakdown AS jsonb),
            nutrition_profiling_details = CAST(:nutrition_profiling_details AS jsonb),
            nutrition_profiling_debug = CAST(:nutrition_profiling_debug AS jsonb),
            total_sustainability = :total_sustainability,
            total_sustainability_per_serving = :total_sustainability_per_serving,
            sustainability_per_kg = :sustainability_per_kg,
            sustainability_profiling_details = CAST(:sustainability_profiling_details AS jsonb),
            trace = CAST(:trace AS jsonb),
            pipeline_version = :pipeline_version,
            computed_at = COALESCE(CAST(:computed_at AS timestamptz), now()),
            updated_at = now()
        WHERE recipe_id = :recipe_id AND nutrition_source = :nutrition_source
    """
    plain_insert_sql = upsert_sql.split("ON CONFLICT", 1)[0]

    def _as_json(value: object) -> str:
        return json.dumps(value if value is not None else None, separators=(",", ":"))

    params = {
        "recipe_id": recipe_id,
        "title": record.get("title"),
        "source": source,
        "source_id": resolved_source_id,
        "nutrition_source": record.get("nutrition_source"),
        "total_nutrients": _as_json(record.get("total_nutrients")),
        "total_nutrients_per_serving": _as_json(record.get("total_nutrients_per_serving")),
        "nutri_score": _as_json(record.get("nutri_score")),
        "nutri_score_breakdown": _as_json(record.get("nutri_score_breakdown")),
        "nutrition_profiling_details": _as_json(record.get("nutrition_profiling_details")),
        "nutrition_profiling_debug": _as_json(record.get("nutrition_profiling_debug")),
        "total_sustainability": record.get("total_sustainability"),
        "total_sustainability_per_serving": record.get("total_sustainability_per_serving"),
        "sustainability_per_kg": record.get("sustainability_per_kg"),
        "sustainability_profiling_details": _as_json(record.get("sustainability_profiling_details")),
        "trace": _as_json(record.get("trace")),
        "pipeline_version": record.get("pipeline_version"),
        "computed_at": record.get("computed_at"),
    }

    try:
        _ensure_profiles_table_ready(create_table_sql, alter_table_sql, ensure_unique_index_sql)
        with get_connection() as conn:
            tx = conn.begin()
            try:
                wrote = False
                if _PROFILES_HAS_UNIQUE:
                    savepoint = conn.begin_nested()
                    try:
                        conn.execute(text(upsert_sql), params)
                        savepoint.commit()
                        wrote = True
                    except SQLAlchemyError:
                        # Constraint vanished since the probe (e.g. index
                        # dropped); fall through to update-then-insert.
                        savepoint.rollback()
                if not wrote:
                    updated = conn.execute(text(update_sql), params)
                    if updated.rowcount == 0:
                        conn.execute(text(plain_insert_sql), params)
                tx.commit()
            except Exception:
                tx.rollback()
                raise
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to upsert recipe profiling trace: {e}") from e


def fetch_ingredient_nutrition_by_canonical_id_irish(canonical_food_id: str) -> Optional[dict]:
    """
    Return Irish ingredient nutrient record from Postgres by canonical food id.

    Args:
        canonical_food_id: Canonical ingredient identifier (e.g. IE00001)

    Returns:
        Full row as dict from the Irish nutrients table, or None if not found.
    """
    cfg = _get_config()

    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT *
            FROM "{cfg['schema']}"."{cfg['irish_ingredients_table']}"
            WHERE "canonical_food_id" = :canonical_food_id
            ORDER BY "row_id" ASC
            LIMIT 1
        ) t
    """

    try:
        with get_connection() as conn:
            result = conn.execute(
                text(query_str), {"canonical_food_id": str(canonical_food_id)}
            )
            row = result.fetchone()
            if row is None:
                return None
            return row[0]
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to fetch Irish ingredient nutrition: {e}") from e


def fetch_ingredient_nutrition_by_canonical_id_hungarian(canonical_food_id: str) -> Optional[dict]:
    """
    Return Hungarian ingredient nutrient record from Postgres by canonical food id.

    Args:
        canonical_food_id: Canonical ingredient identifier (e.g. HU00001)

    Returns:
        Full row as dict from the Hungarian nutrients table, or None if not found.
    """
    cfg = _get_config()

    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT *
            FROM "{cfg['schema']}"."{cfg['hungarian_ingredients_table']}"
            WHERE "canonical_food_id" = :canonical_food_id
            ORDER BY "row_id" ASC
            LIMIT 1
        ) t
    """

    try:
        with get_connection() as conn:
            result = conn.execute(
                text(query_str), {"canonical_food_id": str(canonical_food_id)}
            )
            row = result.fetchone()
            if row is None:
                return None
            return row[0]
    except SQLAlchemyError as e:
        raise RuntimeError(f"Failed to fetch Hungarian ingredient nutrition: {e}") from e


def close_engine():
    """
    Close the database engine and all connections in the pool.
    Call this during application shutdown.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
