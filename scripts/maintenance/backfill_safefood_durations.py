#!/usr/bin/env python3
"""Repair Safefood durations when cook metadata is missing but methods state it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scripts.import_safefood_web import (
    SCRAPE_FILES,
    SOURCE,
    generate_recipe_id,
    parse_minutes,
    resolve_duration,
)
from recipe_wrangler.catalog.projection import project
from recipe_wrangler.repositories.neo4j_recipes import driver


def proposed_updates(titles: set[str] | None = None) -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for path in SCRAPE_FILES:
        for recipe in json.loads(path.read_text(encoding="utf-8")):
            if recipe.get("total_time") or recipe.get("cook_time"):
                continue
            prep = parse_minutes(recipe.get("prep_time"))
            duration = resolve_duration(recipe)
            if duration <= prep:
                continue
            title = str(recipe.get("name") or "").strip()
            if titles and title.casefold() not in titles:
                continue
            updates.append(
                {
                    "recipe_id": generate_recipe_id(title),
                    "title": title,
                    "duration": duration,
                }
            )
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="Only repair this exact recipe title; repeat for multiple recipes.",
    )
    args = parser.parse_args()
    titles = {title.casefold() for title in args.title} or None
    updates = proposed_updates(titles)
    print(f"proposed updates: {len(updates)}")
    for item in updates:
        print(f"{item['title']}: {item['duration']:g} minutes")
    if not args.write:
        return 0

    query = """
    UNWIND $updates AS update
    MATCH (r:Recipe {source: $source})
    WHERE toLower(r.title) = toLower(update.title)
      AND update.duration > coalesce(r.duration, 0)
    SET r.duration = update.duration
    WITH r, update
    OPTIONAL MATCH (r)-[rel:HAS_TAG]->(:Tag {name: "30_minutes_or_less"})
    WHERE update.duration > 30
    DELETE rel
    RETURN collect(r.recipe_id) AS recipe_ids
    """
    with driver.session() as session:
        record = session.run(query, updates=updates, source=SOURCE).single()
        recipe_ids = list(record["recipe_ids"] if record else [])
    try:
        for recipe_id in recipe_ids:
            project(recipe_id)
    finally:
        driver.close()
    print(f"updated and projected: {len(recipe_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
