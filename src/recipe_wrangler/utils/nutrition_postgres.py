# Purpose: Postgres nutrition fetch helpers for API responses.

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

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
        "recipes_table": _env("NUTRITION_RECIPES_TABLE", "nutrients-recipes-usda"),
        "use_docker": _env("NUTRITION_USE_DOCKER", "0") == "1",
        "container": _env("NUTRITION_CONTAINER", _env("POSTGRES_CONTAINER", "")),
    }


def _quote_ident(value: str) -> str:
    return '"{}"'.format(value.replace('"', '""'))


def _sql_literal(value: str) -> str:
    return "'{}'".format(value.replace("'", "''"))


def _run_psql(query: str) -> str:
    cfg = _get_config()
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["db_password"]

    if cfg["use_docker"] and cfg["container"]:
        cmd = [
            "docker",
            "exec",
            "-i",
            cfg["container"],
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-U",
            cfg["db_user"],
            "-d",
            cfg["db_name"],
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
            cfg["db_host"],
            "-p",
            str(cfg["db_port"]),
            "-U",
            cfg["db_user"],
            "-d",
            cfg["db_name"],
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
        msg = result.stderr.strip() or "psql failed"
        raise RuntimeError(msg)

    return result.stdout.strip()


def fetch_ingredient_nutrition_by_usda_id(usda_id: str) -> Optional[dict]:
    """Return ingredient nutrient record from Postgres by USDA id."""
    cfg = _get_config()
    schema_sql = _quote_ident(cfg["schema"])
    table_sql = _quote_ident(cfg["ingredients_table"])
    usda_sql = _sql_literal(str(usda_id))

    query = f"""
SELECT row_to_json(t)
FROM (
  SELECT usda_id, food_name, nutrients
  FROM {schema_sql}.{table_sql}
  WHERE usda_id = {usda_sql}
  LIMIT 1
) t;
"""

    out = _run_psql(query)
    if not out:
        return None
    return json.loads(out)


def fetch_recipe_nutrition_by_id(recipe_id: str) -> Optional[dict]:
    """Return recipe nutrition record from Postgres by recipe id."""
    cfg = _get_config()
    schema_sql = _quote_ident(cfg["schema"])
    table_sql = _quote_ident(cfg["recipes_table"])
    recipe_sql = _sql_literal(str(recipe_id))

    query = f"""
SELECT row_to_json(t)
FROM (
  SELECT recipe_id, title, total_nutrients, nutri_score
  FROM {schema_sql}.{table_sql}
  WHERE recipe_id = {recipe_sql}
  LIMIT 1
) t;
"""

    out = _run_psql(query)
    if not out:
        return None
    return json.loads(out)
