#!/usr/bin/env python3
"""Materialize the `diet_tags` facet -- deterministic only, never scraped.

This is the canonical facet design agreed this session: `diet_tags` answers
"is this recipe safe for this restriction?" and every value in it must be
computed, never trusted from a source dataset. It replaces the old
`vegetarian`/`vegan`/`pescatarian` values that were either scraped as-is
(vegan/vegetarian) or computed by a third, separate keyword+FoodOn
implementation (pescatarian, in scripts/neo4j/tag_recipes.py) -- three
disagreeing systems producing the same-named facts.

Free-from generation abstains; composition tags require supported whole-recipe
assessments. Existing labels are not evidence of verified safety.

This script does not reclassify anything itself; it *reads* already-computed
classifications and materializes them as Tag nodes:

  nut_free, dairy_free, gluten_free
      -> not generated: missing presence evidence does not establish absence.

  pescatarian_safe
      -> current versioned pescatarian SUITABILITY_FOR status="suitable"; legacy
         source labels and absence of meat alone never establish suitability.

  vegan, vegetarian
      -> SUITABILITY_FOR edges with status="suitable" only. Run
         scripts/neo4j/classify_vegan_vegetarian.py --apply first; this
         script only exposes its result as a searchable tag, it does not
         compute suitability itself. Deliberately strict: "unknown" and
         "not_suitable" both stay untagged, matching the safety-first
         posture established this session (never claim positive without
         confidence).

Not recomputed here: `gluten_free_option` -- that stays
scripts/tag_gluten_free_options.py (its own HealthyFoods-specific
description-text detector); run it separately, its output already lands in
the same `dietary_option` category this facet reads.

Usage:
    PYTHONPATH=src python scripts/facets/tag_diet.py            # dry-run
    PYTHONPATH=src python scripts/facets/tag_diet.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from neo4j import GraphDatabase  # noqa: E402

from recipe_wrangler.utils.consumer_suitability import (  # noqa: E402
    SUPPORTED_CONSUMER_GROUPS,
    classification_version,
)
from recipe_wrangler.utils.diet_tags import (  # noqa: E402
    ALLERGEN_ABSENCE_RULES,
)



def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    return GraphDatabase.driver(uri, auth=(user, password))


def _tag_allergen_absence(session, tag_name: str, allergens: list[str], *, apply: bool) -> int:
    # No complete absence/cross-contact assessment exists in this graph.
    # Keep the callable for tooling compatibility, but never materialize safety.
    return 0


def _tag_suitable(session, group: str, *, apply: bool) -> int:
    query = f"""
    MATCH (r:Recipe)-[s:SUITABILITY_FOR]->(:ConsumerGroup {{name: $group}})
    WHERE s.status = "suitable" AND s.classification_version = $version
      AND NOT EXISTS {{ MATCH (r)-[other:SUITABILITY_FOR]->(:ConsumerGroup {{name: $group}})
                       WHERE other.classification_version = $version
                         AND coalesce(other.status, 'unknown') <> 'suitable' }}
    {"MERGE (t:Tag {name: $tag}) ON CREATE SET t.category = 'dietary' "
     "MERGE (r)-[:HAS_TAG]->(t)" if apply else ""}
    RETURN count(DISTINCT r) AS n
    """
    return int(
        session.run(
            query, group=group, tag='pescatarian_safe' if group == 'pescatarian' else group,
            version=classification_version(group)
        ).single()["n"]
    )


def _tag_pescatarian_safe(session, *, apply: bool) -> int:
    """Compatibility entry point using explicit current composition assessments."""
    return _tag_suitable(session, 'pescatarian', apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write. Without this, dry-run only.")
    parser.add_argument(
        "--replace", action="store_true",
        help="Unsupported: clearing dietary edges would erase original source claims.",
    )
    args = parser.parse_args()
    if args.replace:
        parser.error("--replace would erase original source dietary labels; reproject assessed facets instead")

    driver = _driver()
    try:
        with driver.session() as session:
            for tag_name, allergens in ALLERGEN_ABSENCE_RULES.items():
                n = _tag_allergen_absence(session, tag_name, allergens, apply=args.apply)
                print(f"{tag_name}: not generated; absence/cross-contact unassessed")

            for group in SUPPORTED_CONSUMER_GROUPS:
                n = _tag_suitable(session, group, apply=args.apply)
                print(f"{group}: {n} recipes")

        if not args.apply:
            print("dry-run -- nothing written. Re-run with --apply.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
