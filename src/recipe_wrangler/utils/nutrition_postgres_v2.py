# Purpose: Postgres nutrition fetch helpers using SQLAlchemy (proper implementation).

import os
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from sqlalchemy import create_engine, text, Engine
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy.exc import SQLAlchemyError

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None

if load_dotenv:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(dotenv_path=env_path)


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
        "recipes_table": _env("NUTRITION_RECIPES_TABLE", "nutrients-recipes-usda"),
    }


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
        raise RuntimeError(f"Failed to fetch ingredient nutrition: {e}") from e


def fetch_recipe_nutrition_by_id(recipe_id: str) -> Optional[dict]:
    """
    Return recipe nutrition record from Postgres by recipe id.

    Args:
        recipe_id: Recipe identifier

    Returns:
        Dictionary with keys: recipe_id, title, total_nutrients, nutri_score
        None if not found

    Raises:
        SQLAlchemyError: If database query fails
    """
    cfg = _get_config()

    # Build query with proper identifier quoting for schema/table
    query_str = f"""
        SELECT row_to_json(t) as data
        FROM (
            SELECT recipe_id, title, total_nutrients, nutri_score
            FROM "{cfg['schema']}"."{cfg['recipes_table']}"
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
        raise RuntimeError(f"Failed to fetch recipe nutrition: {e}") from e


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


def close_engine():
    """
    Close the database engine and all connections in the pool.
    Call this during application shutdown.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
