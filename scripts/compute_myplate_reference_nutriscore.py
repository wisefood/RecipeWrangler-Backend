#!/usr/bin/env python3
"""Compute reference Nutri-Score for MyPlate recipes from scraped per-serving nutrition.

Uses ingredient weights from existing profiling_details (eu source) to derive
per-serving weight → per-100g → Nutri-Score. Stores result in the 'myplate'
nutrition_source row's nutri_score column.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values

NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

GRADE_TO_LABEL = {"A": "Nutriscore_A", "B": "Nutriscore_B", "C": "Nutriscore_C",
                  "D": "Nutriscore_D", "E": "Nutriscore_E"}
GRADE_TO_COLOR = {"A": "dark green", "B": "green", "C": "yellow",
                  "D": "orange", "E": "dark orange"}


def pg_run(sql: str, fetch=False):
    cmd = ["docker", "exec", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients",
           "-t", "-A", "-F", "\t", "-c", sql]
    out = subprocess.check_output(cmd).decode(errors="replace")
    if not fetch:
        return None
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        rows.append(parts)
    return rows


def pg_exec(sql: str):
    cmd = ["docker", "exec", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients", "-c", sql]
    subprocess.run(cmd, capture_output=True)


def get_serves(recipe_ids: list[str]) -> dict[str, float]:
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as s:
        result = s.run(
            "MATCH (r:Recipe {source: 'MyPlate'}) RETURN r.recipe_id AS rid, r.serves AS serves",
        )
        mapping = {row["rid"]: float(row["serves"] or 1) for row in result}
    driver.close()
    return mapping


def main():
    # Load scraped per-serving nutrients
    print("Loading scraped per-serving nutrients...")
    rows = pg_run("""
SELECT recipe_id, total_nutrients_per_serving::text
FROM "nutrients-recipe-profiles"
WHERE source = 'MyPlate' AND nutrition_source = 'myplate'
  AND total_nutrients_per_serving IS NOT NULL
""", fetch=True)
    per_serving = {}
    for parts in (rows or []):
        if len(parts) == 2:
            per_serving[parts[0]] = json.loads(parts[1])
    print(f"  {len(per_serving)} recipes with scraped nutrition")

    # Load profiling details (ingredient weights) from eu source
    print("Loading ingredient weights from profiling details (eu)...")
    rows = pg_run("""
SELECT recipe_id,
       (SELECT SUM((item->>'weight_g')::float)
        FROM jsonb_array_elements(nutrition_profiling_details) item
        WHERE (item->>'weight_g') IS NOT NULL
          AND (item->>'weight_g')::float > 0) AS total_weight_g
FROM "nutrients-recipe-profiles"
WHERE source = 'MyPlate' AND nutrition_source = 'eu'
  AND nutrition_profiling_details IS NOT NULL
""", fetch=True)
    total_weights: dict[str, float] = {}
    for parts in (rows or []):
        if len(parts) == 2 and parts[1]:
            try:
                total_weights[parts[0]] = float(parts[1])
            except ValueError:
                pass
    print(f"  {len(total_weights)} recipes with ingredient weights")

    # Load serves from Neo4j
    print("Loading serves from Neo4j...")
    serves_map = get_serves(list(per_serving.keys()))

    ok = skipped = 0
    updates: list[tuple[str, dict]] = []

    for recipe_id, nut in per_serving.items():
        total_w = total_weights.get(recipe_id)
        if not total_w or total_w <= 0:
            skipped += 1
            continue
        serves = serves_map.get(recipe_id, 1.0)
        per_serving_w = total_w / serves
        if per_serving_w <= 0:
            skipped += 1
            continue

        kcal = nut.get("energy_kcal") or 0
        values = {
            "energy":         kcal * 4.184 / per_serving_w * 100.0,
            "sugar":          (nut.get("sugar_g") or 0) / per_serving_w * 100.0,
            "saturated_fats": (nut.get("saturated_fat_g") or 0) / per_serving_w * 100.0,
            "sodium":         (nut.get("sodium_mg") or 0) / per_serving_w * 100.0,
            "fibers":         (nut.get("fibre_g") or 0) / per_serving_w * 100.0,
            "proteins":       (nut.get("protein_g") or 0) / per_serving_w * 100.0,
            "fruit_percentage": 0.0,
        }
        try:
            result = compute_nutri_score_breakdown_from_values(values, "solid")
        except Exception:
            skipped += 1
            continue

        raw_grade = str(result.get("nutri_score") or "").replace("Nutriscore_", "").strip().upper()
        if raw_grade not in GRADE_TO_LABEL:
            skipped += 1
            continue

        nutri_score_json = {
            "nutri_score": GRADE_TO_LABEL[raw_grade],
            "score": result.get("score"),
            "color": GRADE_TO_COLOR[raw_grade],
        }
        updates.append((recipe_id, nutri_score_json))
        ok += 1

    print(f"  {ok} computed, {skipped} skipped (no weight data)")

    # Write back to postgres
    print("Writing Nutri-Scores to Postgres...")
    for i, (recipe_id, ns) in enumerate(updates):
        escaped = json.dumps(ns).replace("'", "''")
        sql = f"""
UPDATE "nutrients-recipe-profiles"
SET nutri_score = '{escaped}'::jsonb, updated_at = NOW()
WHERE recipe_id = '{recipe_id}' AND nutrition_source = 'myplate';
"""
        pg_exec(sql)
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(updates)}", flush=True)

    print(f"Done — {ok} MyPlate reference Nutri-Scores stored.")

    # Print grade distribution
    grades = [list(ns.keys())[0] if False else ns["nutri_score"].replace("Nutriscore_", "")
              for _, ns in updates]
    for g in "ABCDE":
        n = grades.count(g)
        print(f"  {g}: {n} ({n/len(grades)*100:.1f}%)")


if __name__ == "__main__":
    main()
