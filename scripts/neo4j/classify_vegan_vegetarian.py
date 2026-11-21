#!/usr/bin/env python3
"""Classify ingredient and recipe composition for vegan, vegetarian and pescatarian use.

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

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.consumer_suitability import (
    DEFINITION_SOURCES,
    SUITABILITY_CLASSIFICATION_VERSION,
    ORIGIN_CLASSIFICATION_VERSION,
    SUPPORTED_CONSUMER_GROUPS,
    DIETARY_RULE_NOTES,
    origin_rows,
    classification_version,
)
from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.food_ontology import CONSUMER_GROUP_IRIS
from recipe_wrangler.utils.dietary_classification import (
    _scalar, _group_params, _initialize_unknown, _classify_negative,
    _classify_positive, _aggregate_recipe_batch, _apply_reviewed, reviewed_diet_rows,
)

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
            "fato_iri": CONSUMER_GROUP_IRIS.get(group),
            "version": classification_version(group),
            "definition_sources": DEFINITION_SOURCES[group],
        }
        for group in SUPPORTED_CONSUMER_GROUPS
    ]
    session.run(
        """
        UNWIND $groups AS item
        MERGE (g:ConsumerGroup {name: item.name})
        SET g.fato_iri = item.fato_iri,
            g.suitability_classification_version = item.version,
            g.definition_sources = item.definition_sources
        """,
        groups=groups,
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
        version=ORIGIN_CLASSIFICATION_VERSION,
    ).consume()


def _write_rule_notes(session) -> None:
    session.run(
        "CREATE CONSTRAINT dietary_rule_note_id IF NOT EXISTS FOR (n:DietaryRuleNote) REQUIRE n.id IS UNIQUE"
    ).consume()
    session.run(
        """
        UNWIND $notes AS item
        MERGE (n:DietaryRuleNote {id: item.id})
        SET n.summary = item.summary, n.foods = item.foods, n.updated_at = datetime()
        WITH n, item
        UNWIND item.groups AS group
        MATCH (g:ConsumerGroup {name: group})
        MERGE (n)-[:ABOUT]->(g)
        """,
        notes=DIETARY_RULE_NOTES,
    ).consume()


def _write_reviewed_table(session) -> None:
    session.run(
        "CREATE CONSTRAINT reviewed_diet_ingredient_name IF NOT EXISTS "
        "FOR (n:ReviewedDietIngredient) REQUIRE n.name IS UNIQUE"
    ).consume()
    session.run(
        """
        UNWIND $rows AS row
        MERGE (n:ReviewedDietIngredient {name: row.name})
        SET n.vegetarian = row.vegetarian, n.vegan = row.vegan, n.pescatarian = row.pescatarian,
            n.reason = row.reason, n.updated_at = datetime()
        """,
        rows=reviewed_diet_rows(),
    ).consume()


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
        version=classification_version(group),
    )
    return {str(row["status"]): int(row["count"]) for row in rows}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify vegan, vegetarian and pescatarian composition."
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
                _write_rule_notes(session)
                _write_reviewed_table(session)
                if not args.skip_ingredients:
                    for group in SUPPORTED_CONSUMER_GROUPS:
                        initialized = _initialize_unknown(session, group)
                        negative = _classify_negative(session, group)
                        positive = _classify_positive(session, group)
                        reviewed = _apply_reviewed(session, group)
                        print(
                            f"{group}: initialized={initialized} "
                            f"not_suitable={negative} suitable={positive} reviewed={reviewed}"
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
