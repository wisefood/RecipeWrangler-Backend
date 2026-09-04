#!/usr/bin/env python3
"""Materialize the `diet_tags` facet -- deterministic only, never scraped.

This is the canonical facet design agreed this session: `diet_tags` answers
"is this recipe safe for this restriction?" and every value in it must be
computed, never trusted from a source dataset. It replaces the old
`vegetarian`/`vegan`/`pescatarian` values that were either scraped as-is
(vegan/vegetarian) or computed by a third, separate keyword+FoodOn
implementation (pescatarian, in scripts/neo4j/tag_recipes.py) -- three
disagreeing systems producing the same-named facts.

This script does not reclassify anything itself; it *reads* already-computed
classifications and materializes them as Tag nodes:

  nut_free, dairy_free, gluten_free
      -> absence of the corresponding allergen(s) on HAS_ALLERGEN edges.
         (Same rule as recipe_wrangler.repositories.neo4j_recipes.infer_diet_tags,
         reimplemented as one Cypher pass instead of per-recipe Python calls.)

  pescatarian_safe
      -> migrated from the existing FoodOn + ingredient-name `pescatarian`
         classification. Fish and seafood are allowed; meat and poultry block
         the tag. It is deliberately not an allergen-absence rule.

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
    PYTHONPATH=src python scripts/facets/tag_diet.py --apply --replace
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
    SUITABILITY_CLASSIFICATION_VERSION,
    SUPPORTED_CONSUMER_GROUPS,
)
from recipe_wrangler.utils.diet_tags import (  # noqa: E402
    ALLERGEN_ABSENCE_RULES,
    DIET_TAG_NAMES,
)


LEGACY_DIET_TAG_NAMES: tuple[str, ...] = (
    "egg_free",
    "soy_free",
    "sesame_free",
    "shellfish_free",
    "pescatarian",
)

PESCATARIAN_FORBIDDEN_ROOTS = ["FOODON_00002671"]
PESCATARIAN_ALLOWED_ROOTS = [
    "FOODON_00001046",  # animal seafood product
    "FOODON_00001248",  # fish food product
    "FOODON_00001293",  # shellfish food product
    "FOODON_00002129",  # plant-based meat analogue
    "FOODON_00002134",  # plant-based seafood analogue
    "FOODON_00002260",  # soybean-based meat analogue
]
PESCATARIAN_FORBIDDEN_FOODON_KEYWORDS = [
    "animal meat", "beef", "pork", "ham", "bacon", "sausage", "chicken",
    "turkey", "duck", "goose", "lamb", "mutton", "veal", "venison",
    "goat",
]
PESCATARIAN_ALLOWED_FOODON_KEYWORDS = [
    "fish", "seafood", "shellfish", "crustacean", "mollusk", "shrimp",
    "prawn", "crab", "lobster", "clam", "mussel", "oyster", "scallop",
    "squid", "octopus",
]
PESCATARIAN_FORBIDDEN_INGREDIENT_KEYWORDS = [
    "beef", "pork", "bacon", "ham", "turkey", "chicken", "duck",
    "goose", "lamb", "mutton", "veal", "venison", "goat", "meat",
    "sausage", "pepperoni", "prosciutto",
]
PESCATARIAN_EXCLUDED_INGREDIENT_KEYWORDS = [
    "plant-based", "plant based", "vegan", "imitation meat",
]


def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    return GraphDatabase.driver(uri, auth=(user, password))


def _tag_allergen_absence(session, tag_name: str, allergens: list[str], *, apply: bool) -> int:
    query = f"""
    MATCH (r:Recipe)
    WHERE EXISTS {{ MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient) }}
      AND NOT EXISTS {{
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
        WHERE a.name IN $allergens
      }}
    {"MERGE (t:Tag {name: $tag_name}) SET t.category = 'dietary' "
     "MERGE (r)-[:HAS_TAG]->(t)" if apply else ""}
    RETURN count(DISTINCT r) AS n
    """
    return int(session.run(query, tag_name=tag_name, allergens=allergens).single()["n"])


def _tag_suitable(session, group: str, *, apply: bool) -> int:
    query = f"""
    MATCH (r:Recipe)-[s:SUITABILITY_FOR]->(:ConsumerGroup {{name: $group}})
    WHERE s.status = "suitable" AND s.classification_version = $version
    {"MERGE (t:Tag {name: $group}) SET t.category = 'dietary' "
     "MERGE (r)-[:HAS_TAG]->(t)" if apply else ""}
    RETURN count(DISTINCT r) AS n
    """
    return int(
        session.run(
            query, group=group, version=SUITABILITY_CLASSIFICATION_VERSION
        ).single()["n"]
    )


def _tag_pescatarian_safe(session, *, apply: bool) -> int:
    """Classify from FoodOn meat roots plus a conservative name backstop."""
    query = f"""
    MATCH (r:Recipe)
    WHERE EXISTS {{ MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient) }}
      AND NOT EXISTS {{
        MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        MATCH (i)-[:HAS_CLASS]->(f:FoodOnClass)
        MATCH (f)-[:SUBCLASS_OF*0..]->(ancestor:FoodOnClass)
        WHERE (
          ancestor.foodon_id IN $forbidden_roots
          OR (
            any(k IN $forbidden_foodon_keywords
                WHERE toLower(coalesce(ancestor.name, "")) CONTAINS k)
            AND NOT any(k IN $allowed_foodon_keywords
                        WHERE toLower(coalesce(ancestor.name, "")) CONTAINS k)
          )
        )
        AND NOT EXISTS {{
          MATCH (i)-[:HAS_CLASS]->(:FoodOnClass)-[:SUBCLASS_OF*0..]->
                (allowed:FoodOnClass)
          WHERE allowed.foodon_id IN $allowed_roots
        }}
      }}
      AND NOT EXISTS {{
        MATCH (r)-[:HAS_INGREDIENT]->(ingredient:Ingredient)
        WHERE any(k IN $forbidden_ingredient_keywords
                  WHERE toLower(coalesce(ingredient.name, "")) CONTAINS k)
          AND NOT any(k IN $excluded_ingredient_keywords
                      WHERE toLower(coalesce(ingredient.name, "")) CONTAINS k)
      }}
    {"MERGE (t:Tag {name: 'pescatarian_safe'}) SET t.category = 'dietary' "
     "MERGE (r)-[:HAS_TAG]->(t)" if apply else ""}
    RETURN count(DISTINCT r) AS n
    """
    return int(
        session.run(
            query,
            forbidden_roots=PESCATARIAN_FORBIDDEN_ROOTS,
            allowed_roots=PESCATARIAN_ALLOWED_ROOTS,
            forbidden_foodon_keywords=PESCATARIAN_FORBIDDEN_FOODON_KEYWORDS,
            allowed_foodon_keywords=PESCATARIAN_ALLOWED_FOODON_KEYWORDS,
            forbidden_ingredient_keywords=(
                PESCATARIAN_FORBIDDEN_INGREDIENT_KEYWORDS
            ),
            excluded_ingredient_keywords=(
                PESCATARIAN_EXCLUDED_INGREDIENT_KEYWORDS
            ),
        ).single()["n"]
    )


def _clear(session, tag_names: list[str]) -> int:
    result = session.run(
        """
        MATCH (:Recipe)-[rel:HAS_TAG]->(t:Tag)
        WHERE t.name IN $names
        DELETE rel
        RETURN count(rel) AS n
        """,
        names=tag_names,
    )
    return int(result.single()["n"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write. Without this, dry-run only.")
    parser.add_argument(
        "--replace", action="store_true",
        help="Delete existing edges for these tags before rebuilding (use when re-running "
        "after a classification change, e.g. the title-check fix).",
    )
    args = parser.parse_args()

    driver = _driver()
    try:
        with driver.session() as session:
            if args.replace:
                if not args.apply:
                    parser.error("--replace requires --apply")
                # Keep gluten_free_option: its explicit-text evidence is owned
                # and regenerated by tag_gluten_free_options.py, not this pass.
                replace_names = [
                    name for name in DIET_TAG_NAMES
                    if name != "gluten_free_option"
                ]
                # `pescatarian` is removed after the replacement classifier
                # runs; it is not an input to that classifier.
                deleted = _clear(session, [
                    *replace_names,
                    *(name for name in LEGACY_DIET_TAG_NAMES
                      if name != "pescatarian"),
                ])
                print(f"deleted existing diet_tags edges: {deleted}")

            for tag_name, allergens in ALLERGEN_ABSENCE_RULES.items():
                n = _tag_allergen_absence(session, tag_name, allergens, apply=args.apply)
                print(f"{tag_name}: {n} recipes")

            for group in SUPPORTED_CONSUMER_GROUPS:
                n = _tag_suitable(session, group, apply=args.apply)
                print(f"{group}: {n} recipes")

            n = _tag_pescatarian_safe(session, apply=args.apply)
            print(f"pescatarian_safe: {n} recipes")

            if args.apply:
                deleted = _clear(session, ["pescatarian"])
                print(f"deleted legacy pescatarian edges: {deleted}")

        if not args.apply:
            print("dry-run -- nothing written. Re-run with --apply.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
