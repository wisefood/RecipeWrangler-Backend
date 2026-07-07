#!/usr/bin/env python3
"""Compute and apply EU nutritional claim tags to all recipes in Neo4j.

Claims (solid foods):
  low_calorie  ≤ 40 kcal per 100g finished dish
  low_fat      ≤ 3g fat per 100g
  high_fibre   ≥ 6g fibre per 100g  OR  ≥ 3g fibre per 100 kcal
  high_protein ≥ 20% of total energy from protein (4 kcal/g)

Also applies:
  vegetarian_or_vegan  — any recipe already tagged vegetarian or vegan
  30_minutes_or_less   — duration_minutes IS NOT NULL AND < 30

Per-100g is computed as:
  total_nutrients / sum(ingredient weight_g in profiling_details) * 100

Source priority per recipe_id:
  safefood_rcsi > safefood_web > planeat > slovenian > recipe1m_original
  > eu > usda > irish > hungarian > safefood
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict

from neo4j import GraphDatabase

NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

SOURCE_PRIORITY = [
    "safefood_rcsi", "safefood_web", "planeat", "slovenian",
    "recipe1m_original", "eu", "usda", "irish", "hungarian", "safefood",
]

BATCH = 500


def pg_fetch_all() -> dict[str, dict]:
    """Return {recipe_id: best_row} where best_row has total_nutrients + weight_g."""
    sql = """
SELECT recipe_id, nutrition_source, total_nutrients, nutrition_profiling_details
FROM "nutrients-recipe-profiles"
WHERE total_nutrients IS NOT NULL
  AND nutrition_profiling_details IS NOT NULL;
"""
    cmd = ["docker", "exec", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients",
           "-t", "-A", "-F", "\x1f", "-c", sql]
    out = subprocess.check_output(cmd).decode(errors="replace")

    priority = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    best: dict[str, tuple[int, dict]] = {}

    for line in out.splitlines():
        parts = line.split("\x1f", 3)
        if len(parts) != 4:
            continue
        rid, nsrc, nuts_raw, details_raw = parts
        try:
            nuts = json.loads(nuts_raw)
            details = json.loads(details_raw)
        except Exception:
            continue
        if not isinstance(details, list):
            continue

        rank = priority.get(nsrc, 99)
        if rid not in best or rank < best[rid][0]:
            best[rid] = (rank, {"recipe_id": rid, "nutrients": nuts, "details": details})

    return {rid: v[1] for rid, v in best.items()}


def compute_per100g(row: dict) -> dict | None:
    total_w = sum((ing.get("weight_g") or 0) for ing in row["details"])
    if total_w <= 0:
        return None
    n = row["nutrients"]
    factor = 100.0 / total_w
    kcal   = (n.get("energy_kcal") or 0) * factor
    fat    = (n.get("fat_g")       or 0) * factor
    fibre  = (n.get("fibre_g")     or 0) * factor
    prot   = (n.get("protein_g")   or 0) * factor
    return {"kcal": kcal, "fat": fat, "fibre": fibre, "protein": prot}


def claim_tags(p: dict, kcal_total: float) -> list[str]:
    tags = []
    if p["kcal"] <= 40:
        tags.append("low_calorie")
    if p["fat"] <= 3:
        tags.append("low_fat")
    fibre_per_100g  = p["fibre"] >= 6
    fibre_per_100kc = (kcal_total > 0) and (p["fibre"] * 100 / p["kcal"] >= 3) if p["kcal"] > 0 else False
    if fibre_per_100g or fibre_per_100kc:
        tags.append("high_fibre")
    prot_energy_pct = (p["protein"] * 4 / p["kcal"]) if p["kcal"] > 0 else 0
    if prot_energy_pct >= 0.20:
        tags.append("high_protein")
    return tags


def neo4j_ensure_tags(driver, tag_names: list[str]):
    with driver.session() as s:
        for name in tag_names:
            s.run("MERGE (:Tag {name: $n})", n=name)


def neo4j_apply_tags(driver, updates: list[tuple[str, list[str]]]):
    """updates: list of (recipe_id, [tag, ...])"""
    for i in range(0, len(updates), BATCH):
        batch = updates[i:i + BATCH]
        with driver.session() as s:
            s.run("""
UNWIND $rows AS row
MATCH (r:Recipe {recipe_id: row.rid})
WITH r, row
UNWIND row.tags AS tname
MERGE (t:Tag {name: tname})
MERGE (r)-[:HAS_TAG]->(t)
""", rows=[{"rid": rid, "tags": tags} for rid, tags in batch])


def neo4j_apply_veg_or_vegan(driver):
    with driver.session() as s:
        result = s.run("""
MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
WHERE t.name IN ['vegetarian', 'vegan']
WITH DISTINCT r
MERGE (tag:Tag {name: 'vegetarian_or_vegan'})
MERGE (r)-[:HAS_TAG]->(tag)
RETURN count(r) AS n
""")
        return result.single()["n"]


def neo4j_apply_quick(driver):
    with driver.session() as s:
        result = s.run("""
MATCH (r:Recipe)
WHERE r.duration_minutes IS NOT NULL AND r.duration_minutes < 30
MERGE (tag:Tag {name: '30_minutes_or_less'})
MERGE (r)-[:HAS_TAG]->(tag)
RETURN count(r) AS n
""")
        return result.single()["n"]


def main():
    print("Fetching nutrition data from Postgres...", flush=True)
    rows = pg_fetch_all()
    print(f"  {len(rows):,} recipes with nutrition data", flush=True)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    # Ensure tag nodes exist
    all_tags = ["low_calorie", "low_fat", "high_fibre", "high_protein",
                "vegetarian_or_vegan", "30_minutes_or_less"]
    neo4j_ensure_tags(driver, all_tags)

    # Compute nutritional claim tags
    updates: list[tuple[str, list[str]]] = []
    counts: dict[str, int] = defaultdict(int)
    skipped = 0

    for rid, row in rows.items():
        p = compute_per100g(row)
        if p is None:
            skipped += 1
            continue
        total_kcal = row["nutrients"].get("energy_kcal") or 0
        tags = claim_tags(p, total_kcal)
        if tags:
            updates.append((rid, tags))
            for t in tags:
                counts[t] += 1

    print(f"  Recipes with tags: {len(updates):,}  |  Skipped (no weight): {skipped:,}")
    for t, n in sorted(counts.items()):
        print(f"    {t}: {n:,}")

    print("Writing nutritional claim tags to Neo4j...", flush=True)
    neo4j_apply_tags(driver, updates)
    print("  Done.")

    print("Applying vegetarian_or_vegan...", flush=True)
    n = neo4j_apply_veg_or_vegan(driver)
    print(f"  {n:,} recipes tagged")

    print("Applying 30_minutes_or_less...", flush=True)
    n = neo4j_apply_quick(driver)
    print(f"  {n:,} recipes tagged")

    driver.close()
    print("All done.")


if __name__ == "__main__":
    main()
