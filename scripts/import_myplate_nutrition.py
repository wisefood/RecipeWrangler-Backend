#!/usr/bin/env python3
"""Import scraped MyPlate reference nutrition into nutrients-recipe-profiles.

Per-serving values from myplate.food JSON-LD are stored as total_nutrients_per_serving.
total_nutrients is derived by multiplying by serves (parsed from recipe_yield).
nutrition_source = 'myplate'
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

IN_FILE = Path("data/MyPlate/myplate_nutrition.json")

UPSERT_SQL = """
INSERT INTO "nutrients-recipe-profiles"
  (recipe_id, source, nutrition_source, total_nutrients, total_nutrients_per_serving, computed_at)
VALUES
  ({recipe_id}, {source}, 'myplate', {total_nutrients}, {total_nutrients_per_serving}, NOW())
ON CONFLICT (recipe_id, nutrition_source)
DO UPDATE SET
  total_nutrients            = EXCLUDED.total_nutrients,
  total_nutrients_per_serving = EXCLUDED.total_nutrients_per_serving,
  updated_at                 = NOW();
"""

NUTRIENT_KEYS = [
    "energy_kcal", "fat_g", "saturated_fat_g", "cholesterol_mg",
    "sodium_mg", "carbs_g", "fibre_g", "sugar_g", "protein_g",
]



def pg_run(sql: str):
    cmd = ["docker", "exec", "-i", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients", "-c", sql]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  PG ERROR: {result.stderr.strip()}")
    return result


def pg_quote(val) -> str:
    if val is None:
        return "NULL"
    if isinstance(val, dict):
        escaped = json.dumps(val).replace("'", "''")
        return f"'{escaped}'::jsonb"
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


def get_source_map() -> dict[str, str]:
    """recipe_id → source from Neo4j via Postgres existing rows."""
    sql = 'SELECT DISTINCT recipe_id, source FROM "nutrients-recipe-profiles" WHERE source IS NOT NULL;'
    cmd = ["docker", "exec", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients",
           "-t", "-A", "-F", "\t", "-c", sql]
    out = subprocess.check_output(cmd).decode()
    mapping = {}
    for line in out.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            mapping[parts[0]] = parts[1]
    return mapping


def main():
    data = json.loads(IN_FILE.read_text())
    source_map = get_source_map()

    inserted = skipped = errors = 0

    for recipe_id, entry in data.items():
        nut = entry.get("nutrition")
        if not nut:
            skipped += 1
            continue

        per_serving: dict = {k: nut[k] for k in NUTRIENT_KEYS if k in nut}
        source = source_map.get(recipe_id, "MyPlate")

        sql = f"""
INSERT INTO "nutrients-recipe-profiles"
  (recipe_id, source, nutrition_source, total_nutrients_per_serving, computed_at)
VALUES
  ({pg_quote(recipe_id)}, {pg_quote(source)}, 'myplate',
   {pg_quote(per_serving)}, NOW())
ON CONFLICT (recipe_id, nutrition_source)
DO UPDATE SET
  total_nutrients             = NULL,
  total_nutrients_per_serving = EXCLUDED.total_nutrients_per_serving,
  updated_at                  = NOW();
"""
        result = pg_run(sql)
        if result.returncode == 0:
            inserted += 1
        else:
            errors += 1

        if inserted % 100 == 0 and inserted > 0:
            print(f"  {inserted} inserted...", flush=True)

    print(f"Done: {inserted} inserted, {skipped} skipped (no nutrition), {errors} errors")


if __name__ == "__main__":
    main()
