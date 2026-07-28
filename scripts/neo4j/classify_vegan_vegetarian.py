#!/usr/bin/env python3
"""Classify ingredient and recipe composition for vegan/vegetarian use.

The command is read-only unless ``--apply`` is supplied. It creates explicit
three-state suitability relationships for every Ingredient and Recipe, so
missing evidence remains queryable as ``unknown``.

Usage:
    PYTHONPATH=src uv run python scripts/neo4j/classify_vegan_vegetarian.py
    PYTHONPATH=src uv run python scripts/neo4j/classify_vegan_vegetarian.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.consumer_suitability import (
    DEFINITION_SOURCES,
    GROUP_RULES,
    POSITIVE_EVIDENCE_EXCLUSIONS,
    SUITABILITY_CLASSIFICATION_VERSION,
    SUPPORTED_CONSUMER_GROUPS,
    VEGAN_NAME_EXCLUSIONS,
    VEGETARIAN_NAME_EXCLUSIONS,
    keyword_regex,
    origin_rows,
)
from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.food_ontology import CONSUMER_GROUP_IRIS

load_runtime_env()


def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    if os.getenv("NEO4J_NO_AUTH") == "1":
        return GraphDatabase.driver(
            uri, auth=None, notifications_min_severity="OFF"
        )
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is required unless NEO4J_NO_AUTH=1")
    return GraphDatabase.driver(
        uri,
        auth=(username, password),
        notifications_min_severity="OFF",
    )


def _scalar(session, query: str, **params: Any) -> int:
    record = session.run(query, **params).single()
    return int(record["count"]) if record else 0


def _ensure_schema_and_origins(session) -> None:
    session.run(
        "CREATE CONSTRAINT consumer_group_name IF NOT EXISTS "
        "FOR (n:ConsumerGroup) REQUIRE n.name IS UNIQUE"
    ).consume()
    session.run(
        "CREATE CONSTRAINT dietary_origin_name IF NOT EXISTS "
        "FOR (n:DietaryOrigin) REQUIRE n.name IS UNIQUE"
    ).consume()

    groups = [
        {
            "name": group,
            "fato_iri": CONSUMER_GROUP_IRIS[group],
            "definition_sources": DEFINITION_SOURCES[group],
        }
        for group in SUPPORTED_CONSUMER_GROUPS
    ]
    session.run(
        """
        UNWIND $groups AS item
        MERGE (g:ConsumerGroup {name: item.name})
        SET g.fato_iri = item.fato_iri,
            g.suitability_classification_version = $version,
            g.definition_sources = item.definition_sources
        """,
        groups=groups,
        version=SUITABILITY_CLASSIFICATION_VERSION,
    ).consume()

    origins = origin_rows()
    session.run(
        """
        UNWIND $origins AS item
        MERGE (origin:DietaryOrigin {name: item.name})
        SET origin.vegan_status = item.vegan_status,
            origin.vegetarian_status = item.vegetarian_status,
            origin.classification_version = $version
        WITH item, origin
        UNWIND item.roots AS root_id
        MATCH (root:FoodOnClass {foodon_id: root_id})
        MERGE (root)-[rel:INDICATES_ORIGIN]->(origin)
        SET rel.classification_version = $version,
            rel.updated_at = datetime()
        """,
        origins=origins,
        version=SUITABILITY_CLASSIFICATION_VERSION,
    ).consume()


def _group_params(group: str) -> dict[str, Any]:
    rule = GROUP_RULES[group]
    return {
        "group": group,
        "blocking_allergens": rule["blocking_allergens"],
        "positive_allergens": rule["positive_allergens"],
        "blocking_keywords": rule["blocking_keywords"],
        "blocking_keyword_regexes": [
            keyword_regex(keyword) for keyword in rule["blocking_keywords"]
        ],
        "positive_keywords": rule["positive_keywords"],
        "positive_keyword_regexes": [
            keyword_regex(keyword) for keyword in rule["positive_keywords"]
        ],
        "negative_exclusions": (
            VEGAN_NAME_EXCLUSIONS
            if group == "vegan"
            else VEGETARIAN_NAME_EXCLUSIONS
        ),
        "positive_exclusions": POSITIVE_EVIDENCE_EXCLUSIONS,
        "definition_sources": DEFINITION_SOURCES[group],
        "version": SUITABILITY_CLASSIFICATION_VERSION,
    }


def _initialize_unknown(session, group: str) -> int:
    return _scalar(
        session,
        """
        MATCH (i:Ingredient)
        MATCH (g:ConsumerGroup {name: $group})
        MERGE (i)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = "unknown",
            rel.scope = "ingredient_composition",
            rel.reason_codes = ["insufficient_evidence"],
            rel.sources = ["rules"],
            rel.definition_sources = $definition_sources,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(i) AS count
        """,
        **_group_params(group),
    )


def _classify_negative(session, group: str) -> int:
    status_property = (
        "vegan_status" if group == "vegan" else "vegetarian_status"
    )
    query = f"""
    MATCH (i:Ingredient)
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(mapped:FoodOnClass)
                   -[:SUBCLASS_OF*0..]->
                   (root:FoodOnClass)-[origin_rel:INDICATES_ORIGIN]->
                   (origin:DietaryOrigin)
    WHERE origin_rel.classification_version = $version
      AND (
        toLower(coalesce(mapped.label, "")) =
          toLower(coalesce(i.name, ""))
        OR toLower(coalesce(mapped.label, "")) STARTS WITH
          toLower(coalesce(i.name, "")) + " ("
        OR toLower(coalesce(i.name, "")) STARTS WITH
          toLower(coalesce(mapped.label, "")) + " ("
      )
    WITH i,
         [value IN collect(DISTINCT CASE
            WHEN origin.{status_property} = "not_suitable"
            THEN origin.name END)
          WHERE value IS NOT NULL] AS blocking_origins
    OPTIONAL MATCH (i)-[:HAS_ALLERGEN]->(allergen:Allergen)
    WITH i, blocking_origins,
         [value IN collect(DISTINCT allergen.name)
          WHERE value IN $blocking_allergens] AS blocking_allergens,
         [idx IN range(0, size($blocking_keywords) - 1)
          WHERE toLower(coalesce(i.name, "")) =~
                $blocking_keyword_regexes[idx]
          | $blocking_keywords[idx]] AS keyword_hits
    WHERE (
      size(blocking_origins) > 0
      OR size(blocking_allergens) > 0
      OR size(keyword_hits) > 0
    )
      AND none(pattern IN $negative_exclusions
               WHERE toLower(coalesce(i.name, "")) =~ pattern)
    MATCH (i)-[rel:SUITABILITY_FOR]->
          (:ConsumerGroup {{name: $group}})
    SET rel.status = "not_suitable",
        rel.scope = "ingredient_composition",
        rel.reason_codes =
          blocking_origins + blocking_allergens + keyword_hits,
        rel.sources =
          ["rules"]
          + CASE WHEN size(blocking_origins) > 0 THEN ["foodon"] ELSE [] END
          + CASE
              WHEN size(blocking_allergens) > 0
              THEN ["allergen_evidence"] ELSE []
            END,
        rel.definition_sources = $definition_sources,
        rel.classification_version = $version,
        rel.updated_at = datetime()
    RETURN count(DISTINCT i) AS count
    """
    return _scalar(session, query, **_group_params(group))


def _classify_positive(session, group: str) -> int:
    status_property = (
        "vegan_status" if group == "vegan" else "vegetarian_status"
    )
    query = f"""
    MATCH (i:Ingredient)
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(:FoodOnClass)-[:SUBCLASS_OF*0..]->
                   (root:FoodOnClass)-[origin_rel:INDICATES_ORIGIN]->
                   (origin:DietaryOrigin)
    WHERE origin_rel.classification_version = $version
    WITH i,
         [value IN collect(DISTINCT CASE
            WHEN origin.{status_property} = "suitable"
            THEN origin.name END)
          WHERE value IS NOT NULL] AS suitable_origins,
         [idx IN range(0, size($positive_keywords) - 1)
          WHERE toLower(coalesce(i.name, "")) =~
                $positive_keyword_regexes[idx]
          | $positive_keywords[idx]] AS keyword_hits
    OPTIONAL MATCH (i)-[:HAS_ALLERGEN]->(allergen:Allergen)
    WITH i, suitable_origins, keyword_hits,
         [value IN collect(DISTINCT allergen.name)
          WHERE value IN $positive_allergens] AS allowed_allergens
    WHERE (
        size(suitable_origins) > 0
        OR size(allowed_allergens) > 0
        OR size(keyword_hits) > 0
      )
      AND none(pattern IN $positive_exclusions
               WHERE toLower(coalesce(i.name, "")) =~ pattern)
    MATCH (i)-[rel:SUITABILITY_FOR]->
          (:ConsumerGroup {{name: $group}})
    WHERE rel.status <> "not_suitable"
    SET rel.status = "suitable",
        rel.scope = "ingredient_composition",
        rel.reason_codes =
          suitable_origins + allowed_allergens + keyword_hits,
        rel.sources =
          ["rules"]
          + CASE WHEN size(suitable_origins) > 0 THEN ["foodon"] ELSE [] END
          + CASE
              WHEN size(allowed_allergens) > 0
              THEN ["allergen_evidence"] ELSE []
            END,
        rel.definition_sources = $definition_sources,
        rel.classification_version = $version,
        rel.updated_at = datetime()
    RETURN count(DISTINCT i) AS count
    """
    return _scalar(session, query, **_group_params(group))


def _recipe_ids(session) -> list[str]:
    return [
        str(record["rid"])
        for record in session.run(
            """
            MATCH (r:Recipe)
            WHERE r.recipe_id IS NOT NULL
            RETURN toString(r.recipe_id) AS rid
            """
        )
    ]


def _aggregate_recipe_batch(
    session,
    recipe_ids: list[str],
    group: str,
) -> int:
    return _scalar(
        session,
        """
        UNWIND $recipe_ids AS rid
        MATCH (r:Recipe {recipe_id: rid})
        MATCH (g:ConsumerGroup {name: $group})
        OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        OPTIONAL MATCH (i)-[ingredient_rel:SUITABILITY_FOR]->(g)
        WHERE ingredient_rel.classification_version = $version
        WITH r, g,
             count(DISTINCT i) AS ingredient_count,
             [value IN collect(DISTINCT CASE
                WHEN ingredient_rel.status = "not_suitable"
                THEN i.name END)
              WHERE value IS NOT NULL] AS blocking_ingredients,
             [value IN collect(DISTINCT CASE
                WHEN ingredient_rel.status IS NULL
                  OR ingredient_rel.status = "unknown"
                THEN i.name END)
              WHERE value IS NOT NULL] AS unknown_ingredients,
             count(DISTINCT CASE
                WHEN ingredient_rel.status = "suitable"
                THEN i END) AS suitable_count
        WITH r, g, blocking_ingredients, unknown_ingredients,
             CASE
               WHEN size(blocking_ingredients) > 0 THEN "not_suitable"
               WHEN ingredient_count > 0
                 AND suitable_count = ingredient_count THEN "suitable"
               ELSE "unknown"
             END AS status
        MERGE (r)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = status,
            rel.scope = "recipe_composition",
            rel.blocking_ingredients = blocking_ingredients,
            rel.unknown_ingredients = unknown_ingredients,
            rel.reason_codes = CASE status
              WHEN "not_suitable" THEN ["blocking_ingredient"]
              WHEN "suitable" THEN ["all_ingredients_suitable"]
              ELSE ["incomplete_ingredient_evidence"] END,
            rel.sources = ["ingredient_suitability"],
            rel.definition_sources = $definition_sources,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(DISTINCT r) AS count
        """,
        recipe_ids=recipe_ids,
        **_group_params(group),
    )


def _status_counts(session, label: str, group: str) -> dict[str, int]:
    rows = session.run(
        f"""
        MATCH (n:{label})-[rel:SUITABILITY_FOR]->
              (:ConsumerGroup {{name: $group}})
        WHERE rel.classification_version = $version
        RETURN rel.status AS status, count(*) AS count
        ORDER BY status
        """,
        group=group,
        version=SUITABILITY_CLASSIFICATION_VERSION,
    )
    return {str(row["status"]): int(row["count"]) for row in rows}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify vegan/vegetarian ingredient and recipe composition."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write classifications. Without this flag, report current counts.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=2000,
        help="Recipes per aggregation transaction.",
    )
    parser.add_argument(
        "--skip-ingredients",
        action="store_true",
        help="Keep current ingredient classifications and rebuild recipes only.",
    )
    parser.add_argument(
        "--skip-recipes",
        action="store_true",
        help="Classify ingredients without rebuilding recipe relationships.",
    )
    args = parser.parse_args()

    graph = _driver()
    try:
        with graph.session() as session:
            ingredients = _scalar(
                session, "MATCH (i:Ingredient) RETURN count(i) AS count"
            )
            recipes = _scalar(
                session, "MATCH (r:Recipe) RETURN count(r) AS count"
            )
            print(
                f"ingredients={ingredients} recipes={recipes} "
                f"version={SUITABILITY_CLASSIFICATION_VERSION} "
                f"mode={'apply' if args.apply else 'dry-run'}"
            )

            if args.apply:
                _ensure_schema_and_origins(session)
                if not args.skip_ingredients:
                    for group in SUPPORTED_CONSUMER_GROUPS:
                        initialized = _initialize_unknown(session, group)
                        negative = _classify_negative(session, group)
                        positive = _classify_positive(session, group)
                        print(
                            f"{group}: initialized={initialized} "
                            f"not_suitable={negative} suitable={positive}"
                        )

                if not args.skip_recipes:
                    recipe_ids = _recipe_ids(session)
                    for group in SUPPORTED_CONSUMER_GROUPS:
                        completed = 0
                        for start in range(
                            0, len(recipe_ids), args.batch_size
                        ):
                            completed += _aggregate_recipe_batch(
                                session,
                                recipe_ids[
                                    start : start + args.batch_size
                                ],
                                group,
                            )
                            if completed % 100000 < args.batch_size:
                                print(
                                    f"{group}: aggregated "
                                    f"{completed}/{len(recipe_ids)} recipes"
                                )

            for group in SUPPORTED_CONSUMER_GROUPS:
                print(
                    f"{group}: ingredient_statuses="
                    f"{_status_counts(session, 'Ingredient', group)}"
                )
                print(
                    f"{group}: recipe_statuses="
                    f"{_status_counts(session, 'Recipe', group)}"
                )
    finally:
        graph.close()


if __name__ == "__main__":
    main()
