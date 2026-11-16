#!/usr/bin/env python3
"""Remove high-confidence FoodOn food-group conflicts from catalog recipes.

Dry-run by default. This intentionally patches only ``food_groups`` and their
annotation evidence; it does not rebuild recipe documents or alter Neo4j.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.catalog import annotation
from recipe_wrangler.catalog.elastic import get_catalog_client
from recipe_wrangler.utils.neo4j_utils import driver


RECIPE_FOODON_QUERY = """
MATCH (r:Recipe)
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
  RETURN collect(DISTINCT i.name) AS ingredient_names
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)-[:HAS_CLASS]->(direct:FoodOnClass)
  OPTIONAL MATCH path=(direct)-[:SUBCLASS_OF*0..5]->(anc:FoodOnClass)
  RETURN collect(DISTINCT CASE WHEN anc IS NULL THEN NULL ELSE {
    ingredient: i.name, ingredient_id: i.canonical_id,
    foodon_id: anc.foodon_id, distance: length(path)
  } END) AS ingredient_foodon_paths
}
RETURN coalesce(r.recipe_id, r.id) AS recipe_id, r.title AS title,
       ingredient_names, ingredient_foodon_paths
"""


def proposed_updates() -> tuple[list[tuple[str, dict]], Counter[str], list[dict]]:
    client = get_catalog_client()
    alias = get_settings().catalog_recipes_alias
    indexed: dict[str, tuple[str, dict]] = {}
    for hit in client.scroll_all(
        alias,
        source=["recipe_id", "title", "food_groups", "annotation_evidence"],
    ):
        source = hit.get("_source") or {}
        indexed[str(source.get("recipe_id"))] = (str(hit["_id"]), source)

    with driver.session() as session:
        owner_rows = session.run(RECIPE_FOODON_QUERY).data()

    updates: list[tuple[str, dict]] = []
    removed: Counter[str] = Counter()
    examples: list[dict] = []
    for row in owner_rows:
        indexed_row = indexed.get(str(row.get("recipe_id")))
        if not indexed_row:
            continue
        document_id, current = indexed_row
        before = set(current.get("food_groups") or [])
        groups, food_group_evidence = annotation.derive_food_groups(
            {
                "ingredient_foodon_paths": row.get("ingredient_foodon_paths") or [],
                "ingredient_names": row.get("ingredient_names") or [],
                "existing_food_groups": sorted(before),
                "existing_annotation_evidence": current.get("annotation_evidence") or [],
            }
        )
        after = set(groups)
        # This maintenance pass is intentionally corrective-only. New group
        # discovery belongs to a reviewed catalog rebuild, not a cleanup of
        # known contradictory historical values.
        if not before - after:
            continue
        removed.update(before - after)
        evidence = [
            item
            for item in current.get("annotation_evidence") or []
            if item.get("facet") != "food_groups"
        ] + food_group_evidence
        updates.append(
            (document_id, {"food_groups": groups, "annotation_evidence": evidence})
        )
        examples.append(
            {
                "recipe_id": row.get("recipe_id"),
                "title": row.get("title"),
                "removed": sorted(before - after),
            }
        )
    return updates, removed, examples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--examples", type=int, default=20)
    args = parser.parse_args()

    updates, removed, examples = proposed_updates()
    print(f"proposed updates: {len(updates)}")
    print(f"groups removed: {dict(sorted(removed.items()))}")
    for example in examples[: max(args.examples, 0)]:
        print(
            f"{example['recipe_id']} {example['title']}: "
            f"remove {', '.join(example['removed'])}"
        )
    if not args.write:
        return 0

    client = get_catalog_client()
    alias = get_settings().catalog_recipes_alias
    succeeded, failures = client.bulk_update(alias, updates, refresh=True)
    print(f"updated: {succeeded}; failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
