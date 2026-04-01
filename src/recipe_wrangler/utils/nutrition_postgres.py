# Purpose: Postgres nutrition fetch helpers using SQLAlchemy with docker psql fallback.

import json
import os
import subprocess
from typing import Optional
from contextlib import contextmanager

from sqlalchemy import create_engine, text, Engine
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy.exc import SQLAlchemyError
from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()


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
        # pool_size=5: Keep 5 connections in the pool
        # max_overflow=10: Allow up to 10 additional connections when pool is full
        # pool_pre_ping=True: Test connections before using them (handles stale connections)
        _engine = create_engine(
            connection_url,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
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


def fetch_recipe_nutrition_by_id(recipe_id: str) -> Optional[dict]:
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
            SELECT recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = :recipe_id
            LIMIT 1
        ) t
    """

    try:
        with get_connection() as conn:
            result = conn.execute(text(query_str), {"recipe_id": str(recipe_id)})
            row = result.fetchone()

            if row is None:
                return None

            return row[0]

    except SQLAlchemyError as e:
        if cfg["use_docker"] and cfg["container"]:
            query = f"""
                SELECT row_to_json(t)
                FROM (
                    SELECT recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source
                    FROM "{cfg['schema']}"."{cfg['profiles_table']}"
                    WHERE recipe_id = '{str(recipe_id).replace("'", "''")}'
                    LIMIT 1
                ) t
            """
            out = _run_psql_fallback(query, cfg)
            if not out:
                return None
            return json.loads(out)
        raise RuntimeError(f"Failed to fetch recipe nutrition: {e}") from e


def fetch_recipe_profiling_trace_by_id(recipe_id: str) -> Optional[dict]:
    """
    Return recipe profiling trace from Postgres by recipe id.

    Returns full row as dict from profiles table, or None if not found.
    """
    cfg = _get_config()
    query_str = f"""
        SELECT row_to_json(t) as data
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
                trace,
                pipeline_version,
                mapping_version,
                embedding_model,
                ruleset_version,
                computed_at,
                updated_at
            FROM "{cfg['schema']}"."{cfg['profiles_table']}"
            WHERE recipe_id = :recipe_id
            LIMIT 1
        ) t
    """
    try:
        with get_connection() as conn:
            result = conn.execute(text(query_str), {"recipe_id": str(recipe_id)})
            row = result.fetchone()
            if row is None:
                return None
            return row[0]
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
                        trace,
                        pipeline_version,
                        mapping_version,
                        embedding_model,
                        ruleset_version,
                        computed_at,
                        updated_at
                    FROM "{cfg['schema']}"."{cfg['profiles_table']}"
                    WHERE recipe_id = '{str(recipe_id).replace("'", "''")}'
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
      pipeline_version, mapping_version, embedding_model, ruleset_version,
      computed_at (optional, timestamptz-compatible string)
    """
    recipe_id = str(record.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError("upsert_recipe_profiling_trace requires recipe_id")

    cfg = _get_config()
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS "{cfg['schema']}"."{cfg['profiles_table']}" (
            recipe_id text NOT NULL,
            nutrition_source text NOT NULL,
            title text,
            source text,
            total_nutrients jsonb,
            total_nutrients_per_serving jsonb,
            nutri_score jsonb,
            nutri_score_breakdown jsonb,
            nutrition_profiling_details jsonb,
            nutrition_profiling_debug jsonb,
            trace jsonb,
            pipeline_version text,
            mapping_version text,
            embedding_model text,
            ruleset_version text,
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
            nutrition_source,
            total_nutrients,
            total_nutrients_per_serving,
            nutri_score,
            nutri_score_breakdown,
            nutrition_profiling_details,
            nutrition_profiling_debug,
            trace,
            pipeline_version,
            mapping_version,
            embedding_model,
            ruleset_version,
            computed_at,
            updated_at
        )
        VALUES (
            :recipe_id,
            :title,
            :source,
            :nutrition_source,
            CAST(:total_nutrients AS jsonb),
            CAST(:total_nutrients_per_serving AS jsonb),
            CAST(:nutri_score AS jsonb),
            CAST(:nutri_score_breakdown AS jsonb),
            CAST(:nutrition_profiling_details AS jsonb),
            CAST(:nutrition_profiling_debug AS jsonb),
            CAST(:trace AS jsonb),
            :pipeline_version,
            :mapping_version,
            :embedding_model,
            :ruleset_version,
            COALESCE(CAST(:computed_at AS timestamptz), now()),
            now()
        )
        ON CONFLICT (recipe_id, nutrition_source) DO UPDATE SET
            title = EXCLUDED.title,
            source = EXCLUDED.source,
            total_nutrients = EXCLUDED.total_nutrients,
            total_nutrients_per_serving = EXCLUDED.total_nutrients_per_serving,
            nutri_score = EXCLUDED.nutri_score,
            nutri_score_breakdown = EXCLUDED.nutri_score_breakdown,
            nutrition_profiling_details = EXCLUDED.nutrition_profiling_details,
            nutrition_profiling_debug = EXCLUDED.nutrition_profiling_debug,
            trace = EXCLUDED.trace,
            pipeline_version = EXCLUDED.pipeline_version,
            mapping_version = EXCLUDED.mapping_version,
            embedding_model = EXCLUDED.embedding_model,
            ruleset_version = EXCLUDED.ruleset_version,
            computed_at = EXCLUDED.computed_at,
            updated_at = now()
    """

    def _as_json(value: object) -> str:
        return json.dumps(value if value is not None else None, separators=(",", ":"))

    params = {
        "recipe_id": recipe_id,
        "title": record.get("title"),
        "source": record.get("source"),
        "nutrition_source": record.get("nutrition_source"),
        "total_nutrients": _as_json(record.get("total_nutrients")),
        "total_nutrients_per_serving": _as_json(record.get("total_nutrients_per_serving")),
        "nutri_score": _as_json(record.get("nutri_score")),
        "nutri_score_breakdown": _as_json(record.get("nutri_score_breakdown")),
        "nutrition_profiling_details": _as_json(record.get("nutrition_profiling_details")),
        "nutrition_profiling_debug": _as_json(record.get("nutrition_profiling_debug")),
        "trace": _as_json(record.get("trace")),
        "pipeline_version": record.get("pipeline_version"),
        "mapping_version": record.get("mapping_version"),
        "embedding_model": record.get("embedding_model"),
        "ruleset_version": record.get("ruleset_version"),
        "computed_at": record.get("computed_at"),
    }

    try:
        with get_connection() as conn:
            tx = conn.begin()
            try:
                conn.execute(text(create_table_sql))
                conn.execute(text(upsert_sql), params)
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
