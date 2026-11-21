#!/usr/bin/env python3
"""Restore FoodHero total durations from its structured prep/cook metadata."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.neo4j.import_foodhero_recipes_to_neo4j import _parse_duration_minutes
from recipe_wrangler.catalog.projection import project
from recipe_wrangler.repositories.neo4j_recipes import driver

INPUT = REPO_ROOT / "data" / "recipes" / "foodhero" / "foodhero_recipes_clean.json"


def proposed_updates() -> list[dict[str, object]]:
    recipes = json.loads(INPUT.read_text(encoding="utf-8"))
    return [
        {
            "title": str(recipe.get("title") or title),
            "url": str(recipe.get("url") or ""),
            "duration": _parse_duration_minutes(recipe.get("duration")),
        }
        for title, recipe in recipes.items()
        if _parse_duration_minutes(recipe.get("duration")) is not None
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    updates = proposed_updates()

    query = """
    UNWIND $updates AS update
    MATCH (r:Recipe {source: "FoodHero"})
    WHERE (update.url <> "" AND r.url = update.url)
       OR toLower(r.title) = toLower(update.title)
    WITH r, update, r.duration AS old_duration
    WHERE old_duration IS NULL OR abs(old_duration - update.duration) > 0.001
    SET r.duration = update.duration
    WITH r, update, old_duration
    OPTIONAL MATCH (r)-[rel:HAS_TAG]->(:Tag {name: "30_minutes_or_less"})
    WHERE update.duration > 30
    DELETE rel
    RETURN r.recipe_id AS recipe_id, r.title AS title,
           old_duration, update.duration AS duration
    """
    with driver.session() as session:
        changed = [dict(row) for row in session.run(query, updates=updates)] if args.write else []
        if not args.write:
            count = session.run(
                """
                UNWIND $updates AS update
                MATCH (r:Recipe {source: "FoodHero"})
                WHERE ((update.url <> "" AND r.url = update.url)
                   OR toLower(r.title) = toLower(update.title))
                  AND (r.duration IS NULL OR abs(r.duration - update.duration) > 0.001)
                RETURN count(DISTINCT r) AS count
                """,
                updates=updates,
            ).single()["count"]
            print(f"proposed updates: {count}")
            driver.close()
            return 0

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda row: project(row["recipe_id"]), changed))
    finally:
        driver.close()
    print(f"updated and projected: {len(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
