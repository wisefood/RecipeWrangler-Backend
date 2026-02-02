# Purpose: Postgres nutrition fetch helpers for API responses.

import json
import os
import subprocess
from typing import Optional


DB_NAME = os.getenv("NUTRITION_DB", "nutrition")
DB_USER = os.getenv("NUTRITION_USER", "postgres")
DB_PASSWORD = os.getenv("NUTRITION_PASSWORD", "postgres")
DB_HOST = os.getenv("NUTRITION_HOST", "localhost")
DB_PORT = os.getenv("NUTRITION_PORT", "5432")
SCHEMA = os.getenv("NUTRITION_SCHEMA", "public")
INGREDIENTS_TABLE = os.getenv("NUTRITION_INGREDIENTS_TABLE", "nutrients-ingredients-usda")
RECIPES_TABLE = os.getenv("NUTRITION_RECIPES_TABLE", "nutrients-recipes-usda")


def _quote_ident(value: str) -> str:
    return '"{}"'.format(value.replace('"', '""'))


def _sql_literal(value: str) -> str:
    return "'{}'".format(value.replace("'", "''"))


def _run_psql(query: str) -> str:
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD

    cmd = [
        "psql",
        "-X",
        "-q",
        "-t",
        "-A",
        "-h",
        DB_HOST,
        "-p",
        str(DB_PORT),
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
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
    schema_sql = _quote_ident(SCHEMA)
    table_sql = _quote_ident(INGREDIENTS_TABLE)
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
    schema_sql = _quote_ident(SCHEMA)
    table_sql = _quote_ident(RECIPES_TABLE)
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
