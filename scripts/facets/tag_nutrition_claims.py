#!/usr/bin/env python3
"""Materialize the `nutrition_claims` facet -- deterministic, EU nutrient-threshold rules.

Consolidates scripts/tag_nutritional_claims.py and scripts/tag_healthy_nutritious.py
into one canonical script for this facet, and fixes the bug both had: neither ever
set a `category` on the Tag nodes it created, so `low_fat`/`high_protein`/
`high_fibre`/`low_calorie`/`healthy_and_nutritious` were real, correctly-computed
facts that silently fell outside any category-filtered facet (e.g. `diet_tags`,
which only reads `category IN ['dietary', 'dietary_option']`). This script sets
`category = 'nutrition_claim'` so they resolve to their own facet, separate from
`diet_tags` -- these are nutrient-content claims, not eligibility/safety facts,
and mixing the two axes was part of what made the old `dietary` category a junk
drawer.

Also drops what the old scripts bundled in but don't belong to this facet:
`vegetarian_or_vegan` (removed from the design entirely -- derivable at query
time, not worth storing) and the former `30_minutes_or_less` tag (now the
separate, product-facing `convenience: quick` facet, derived deterministically
from the existing `duration` field).

Claims (solid foods):
  low_calorie          <= 40 kcal per 100g finished dish
  low_fat               <= 3g fat per 100g
  high_fibre            >= 6g fibre per 100g  OR  >= 3g fibre per 100 kcal
  high_protein          >= 20% of total energy from protein (4 kcal/g)
  healthy_and_nutritious -> Nutri-Score A, from the highest-priority region source

Usage:
    PYTHONPATH=src python scripts/facets/tag_nutrition_claims.py            # dry-run
    PYTHONPATH=src python scripts/facets/tag_nutrition_claims.py --apply
    PYTHONPATH=src python scripts/facets/tag_nutrition_claims.py --apply --replace
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from neo4j import GraphDatabase  # noqa: E402

from recipe_wrangler.utils.nutrition_claims import (  # noqa: E402
    NUTRITION_CLAIM_TAG_NAMES as NUTRITION_CLAIM_TAGS,
    compute_nutrition_claim_tags,
)
from recipe_wrangler.catalog.entities import recipe_entity  # noqa: E402

SOURCE_PRIORITY = [
    "safefood_rcsi", "safefood_web", "planeat", "slovenian_original",
    "eu", "irish", "hungarian", "safefood",
]

BATCH = 500


def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    return GraphDatabase.driver(uri, auth=(user, password))


def _pg(sql: str) -> str:
    cmd = [
        "docker", "exec", os.getenv("NUTRITION_CONTAINER", "wisefood-postgres"),
        "psql", "-U", os.getenv("NUTRITION_USER", "postgres"),
        "-d", os.getenv("NUTRITION_DB", "nutrients"),
        "-t", "-A", "-F", "\x1f", "-c", sql,
    ]
    return subprocess.check_output(cmd).decode(errors="replace")


def pg_fetch_nutrient_rows() -> dict[str, dict]:
    """Return {recipe_id: best_row} -- highest-priority source per recipe_id."""
    sql = """
        SELECT recipe_id, nutrition_source, total_nutrients, nutrition_profiling_details
        FROM "nutrients-recipe-profiles"
        WHERE total_nutrients IS NOT NULL AND nutrition_profiling_details IS NOT NULL;
    """
    priority = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    best: dict[str, tuple[int, dict]] = {}
    for line in _pg(sql).splitlines():
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
            best[rid] = (rank, {"nutrients": nuts, "details": details})
    return {rid: v[1] for rid, v in best.items()}


def pg_fetch_nutriscore_a_ids() -> list[str]:
    sql = """
        WITH ranked AS (
          SELECT recipe_id, nutri_score->>'nutri_score' AS grade,
                 ROW_NUMBER() OVER (
                   PARTITION BY recipe_id
                   ORDER BY CASE nutrition_source
                     WHEN 'safefood_rcsi' THEN 1 WHEN 'safefood_web' THEN 2
                     WHEN 'planeat' THEN 3 WHEN 'slovenian' THEN 4
                     WHEN 'eu' THEN 5 WHEN 'irish' THEN 6
                     WHEN 'hungarian' THEN 7 ELSE 8 END
                 ) AS rn
          FROM "nutrients-recipe-profiles"
          WHERE nutri_score IS NOT NULL AND nutri_score->>'nutri_score' != ''
        )
        SELECT recipe_id FROM ranked WHERE rn = 1 AND grade = 'Nutriscore_A';
    """
    return [line.strip() for line in _pg(sql).splitlines() if line.strip()]


def es_fetch_liquid_recipe_ids() -> set[str]:
    """Use the v4 course facet so EU liquid thresholds apply to beverages."""
    entity = recipe_entity()
    return {
        str((hit.get("_source") or {}).get("recipe_id") or "").strip()
        for hit in entity.scroll_all(
            query={"term": {"course_types": "beverages"}},
            source=["recipe_id"],
        )
        if str((hit.get("_source") or {}).get("recipe_id") or "").strip()
    }


def _apply_tags(session, updates: list[tuple[str, list[str]]]) -> None:
    for i in range(0, len(updates), BATCH):
        batch = updates[i : i + BATCH]
        session.run(
            """
            UNWIND $rows AS row
            MATCH (r:Recipe {recipe_id: row.rid})
            UNWIND row.tags AS tname
            MERGE (t:Tag {name: tname})
            SET t.category = 'nutrition_claim'
            MERGE (r)-[:HAS_TAG]->(t)
            """,
            rows=[{"rid": rid, "tags": tags} for rid, tags in batch],
        )


def _apply_healthy(session, recipe_ids: list[str]) -> None:
    for i in range(0, len(recipe_ids), BATCH):
        batch = recipe_ids[i : i + BATCH]
        session.run(
            """
            UNWIND $ids AS rid
            MATCH (r:Recipe {recipe_id: rid})
            MERGE (t:Tag {name: 'healthy_and_nutritious'})
            SET t.category = 'nutrition_claim'
            MERGE (r)-[:HAS_TAG]->(t)
            """,
            ids=batch,
        )


def _clear(session) -> int:
    result = session.run(
        """
        MATCH (:Recipe)-[rel:HAS_TAG]->(t:Tag)
        WHERE t.name IN $names
        DELETE rel
        RETURN count(rel) AS n
        """,
        names=list(NUTRITION_CLAIM_TAGS),
    )
    return int(result.single()["n"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write. Without this, dry-run only.")
    parser.add_argument(
        "--replace", action="store_true",
        help="Delete existing edges for these tags before rebuilding (e.g. to pick up "
        "the category fix on tags computed by the old scripts).",
    )
    args = parser.parse_args()

    print("Fetching nutrition data from Postgres...", flush=True)
    nutrient_rows = pg_fetch_nutrient_rows()
    liquid_recipe_ids = es_fetch_liquid_recipe_ids()
    print(f"  {len(nutrient_rows):,} recipes with nutrition data")

    updates: list[tuple[str, list[str]]] = []
    counts: dict[str, int] = defaultdict(int)
    skipped = 0
    for rid, row in nutrient_rows.items():
        if not any((ing.get("weight_g") or ing.get("weight")) for ing in row["details"]):
            skipped += 1
            continue
        tags = compute_nutrition_claim_tags(
            row["nutrients"],
            row["details"],
            physical_form="liquid" if rid in liquid_recipe_ids else "solid",
        )
        if tags:
            updates.append((rid, tags))
            for t in tags:
                counts[t] += 1
    print(f"  recipes with claim tags: {len(updates):,}  |  skipped (no weight): {skipped:,}")
    for t, n in sorted(counts.items()):
        print(f"  {t}: {n:,}")

    print("Fetching Nutri-Score A recipes...", flush=True)
    healthy_ids = pg_fetch_nutriscore_a_ids()
    print(f"  healthy_and_nutritious: {len(healthy_ids):,}")

    if not args.apply:
        print("dry-run -- nothing written. Re-run with --apply.")
        return

    driver = _driver()
    try:
        with driver.session() as session:
            if args.replace:
                deleted = _clear(session)
                print(f"deleted existing nutrition_claims edges: {deleted}")
            print("Writing claim tags...", flush=True)
            _apply_tags(session, updates)
            print("Writing healthy_and_nutritious...", flush=True)
            _apply_healthy(session, healthy_ids)
    finally:
        driver.close()
    print("Done.")


if __name__ == "__main__":
    main()
