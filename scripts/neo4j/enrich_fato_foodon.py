#!/usr/bin/env python3
"""Enrich the existing food graph with selected FATO/FoodOn semantics.

This migration is additive and idempotent:

* existing Ingredient, Allergen, FoodOnClass, and HAS_ALLERGEN data is kept;
* Allergen nodes receive canonical FATO/FoodOn crosswalk properties;
* existing HAS_ALLERGEN edges receive versioned evidence metadata;
* existing Ingredient nodes receive conservative SUITABILITY_FOR edges to
  FATO ConsumerGroup nodes.

Missing suitability evidence remains unknown. Run without ``--apply`` for a
read-only preview.

Usage:
    PYTHONPATH=src python scripts/neo4j/enrich_fato_foodon.py
    PYTHONPATH=src python scripts/neo4j/enrich_fato_foodon.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.food_ontology import (
    ALLERGEN_ONTOLOGY_MAPPINGS,
    CLASSIFICATION_VERSION,
    CONSUMER_GROUP_IRIS,
    FATO_ALLERGEN_CLASS_IRI,
    FATO_ALLERGEN_DECLARATION_CLASS_IRI,
)

load_runtime_env()


PLANT_FOOD_ROOTS = [
    "FOODON_00001015",  # plant food product
    "FOODON_00002129",  # plant-based meat product analog
    "FOODON_00002134",  # plant-based seafood product analog
    "FOODON_00002260",  # soybean-based meat product analog
]

GROUP_RULES: dict[str, dict[str, list[str]]] = {
    "vegan": {
        "blocking_allergens": [
            "milk", "egg", "fish", "crustacean_shellfish", "molluscs"
        ],
        "blocking_roots": [
            "FOODON_00002671",  # animal meat food product
            "FOODON_00001046",  # animal seafood product
            "FOODON_00001248",  # fish food product
            "FOODON_00001293",  # shellfish food product
            "FOODON_00001256",  # dairy food product
            "FOODON_00001274",  # egg food product
            "FOODON_00001900",  # gelatin refined food product
            "FOODON_00001899",  # gelatin dessert food product
            "CHEBI_5291",       # gelatin
        ],
        "blocking_keywords": [
            "beef", "pork", "bacon", "ham", "turkey", "chicken", "duck",
            "goose", "lamb", "mutton", "veal", "venison", "goat", "meat",
            "sausage", "pepperoni", "prosciutto", "fish", "salmon", "tuna",
            "cod", "shrimp", "prawn", "crab", "lobster", "shellfish",
            "milk", "cheese", "butter", "cream", "yogurt", "gelatin",
            "whey", "casein", "honey",
        ],
        "positive_roots": PLANT_FOOD_ROOTS,
        "positive_keywords": ["vegan", "plant based", "plant-based"],
    },
    "vegetarian": {
        "blocking_allergens": [
            "fish", "crustacean_shellfish", "molluscs"
        ],
        "blocking_roots": [
            "FOODON_00002671",
            "FOODON_00001046",
            "FOODON_00001248",
            "FOODON_00001293",
            "FOODON_00001900",
            "FOODON_00001899",
            "CHEBI_5291",
        ],
        "blocking_keywords": [
            "beef", "pork", "bacon", "ham", "turkey", "chicken", "duck",
            "goose", "lamb", "mutton", "veal", "venison", "goat", "meat",
            "sausage", "pepperoni", "prosciutto", "fish", "salmon", "tuna",
            "cod", "shrimp", "prawn", "crab", "lobster", "shellfish",
            "gelatin",
        ],
        "positive_roots": PLANT_FOOD_ROOTS,
        "positive_keywords": [
            "vegan", "vegetarian", "plant based", "plant-based"
        ],
    },
    "coeliac": {
        "blocking_allergens": ["gluten", "wheat"],
        "blocking_roots": [
            "FOODON_03420177", "FOODON_00001907", "FOODON_03310809",
            "FOODON_00001275", "FOODON_00001217", "FOODON_00001272",
        ],
        "blocking_keywords": [
            "gluten", "wheat", "barley", "rye", "spelt", "kamut", "farro",
            "durum", "bulgur", "malt", "seitan", "flour", "bread", "pasta",
            "noodle", "oats",
        ],
        "positive_roots": [],
        "positive_keywords": ["gluten free", "gluten-free"],
    },
}

PLANT_ALTERNATIVE_PATTERNS = [
    r".*\b(vegan|vegetarian|plant[ -]*based)\b.*",
    r".*\b(coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"([ -]+(flavoured|flavored))?[ -]+(milk|cream|yogurt|yoghurt)\b.*",
    r".*\b(non[ -]*dairy|dairy[ -]*free)\b.*",
    r".*\b(peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter\b.*",
    r".*\bbutter[ -]*beans?\b.*",
    r".*\bbutternut\b.*",
]

GLUTEN_SAFE_PATTERNS = [
    r".*\bgluten[ -]*free\b.*",
    r"^gluten[ -]+(baking flour|self raising flour|flour|soy sauce|bread|"
    r"pasta|flour almond coconut|flour mix|bread mix)$",
    r".*\bbuckwheat\b.*",
    r".*\b(rice|tapioca|potato|almond|coconut|besan|chickpea|corn|maize|"
    r"quinoa|cassava|arrowroot)[ -]+flour\b.*",
    r".*\b(rice|pulse|chickpea|corn|maize|quinoa)[ -]+"
    r"(noodles?|pasta|spaghetti)\b.*",
    r".*\btamari\b.*",
    r".*\bpasta[ -]+sauce\b.*",
    r"^(ground|minced|fresh|crystallized|crystallised|pickled|glace)?"
    r"[ -]*ginger$",
    r".*\b(wine|vinegar|vinaigrette)\b.*",
]

POSITIVE_EVIDENCE_EXCLUSIONS = [
    r"^\s*\*?\s*note\b.*",
    r".*\bingredients? with an asterisk\b.*",
    r".*\busually gluten[ -]*free\b.*",
    r".*\bcheck the label\b.*",
]


def _keyword_regex(keyword: str) -> str:
    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"[\s-]+")
    return rf".*\b{escaped}(e?s)?\b.*"


def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    if os.getenv("NEO4J_NO_AUTH") == "1":
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is required unless NEO4J_NO_AUTH=1")
    return GraphDatabase.driver(uri, auth=(username, password))


def _scalar(session, query: str, **params: Any) -> int:
    record = session.run(query, **params).single()
    return int(record["count"]) if record else 0


def _ensure_schema(session) -> None:
    statements = [
        (
            "CREATE CONSTRAINT consumer_group_name IF NOT EXISTS "
            "FOR (n:ConsumerGroup) REQUIRE n.name IS UNIQUE"
        ),
        (
            "CREATE CONSTRAINT allergen_name IF NOT EXISTS "
            "FOR (n:Allergen) REQUIRE n.name IS UNIQUE"
        ),
        (
            "CREATE CONSTRAINT allergen_declaration_id IF NOT EXISTS "
            "FOR (n:AllergenDeclaration) REQUIRE n.declaration_id IS UNIQUE"
        ),
    ]
    for statement in statements:
        session.run(statement).consume()


def _upsert_ontology_crosswalk(session) -> None:
    allergens = [
        {
            "name": name,
            "foodon_label_claim_id": mapping.foodon_label_claim_id,
            "eu_label": mapping.eu_label,
        }
        for name, mapping in ALLERGEN_ONTOLOGY_MAPPINGS.items()
    ]
    session.run(
        """
        UNWIND $allergens AS item
        MERGE (a:Allergen {name: item.name})
        SET a.canonical_id = item.name,
            a.eu_label = item.eu_label,
            a.jurisdiction = "EU",
            a.fato_class_iri = $fato_allergen_class_iri,
            a.foodon_label_claim_id = item.foodon_label_claim_id,
            a.classification_version = $version
        """,
        allergens=allergens,
        fato_allergen_class_iri=FATO_ALLERGEN_CLASS_IRI,
        version=CLASSIFICATION_VERSION,
    ).consume()

    groups = [
        {"name": name, "fato_iri": fato_iri}
        for name, fato_iri in CONSUMER_GROUP_IRIS.items()
    ]
    session.run(
        """
        UNWIND $groups AS item
        MERGE (g:ConsumerGroup {name: item.name})
        SET g.fato_iri = item.fato_iri,
            g.classification_version = $version
        """,
        groups=groups,
        version=CLASSIFICATION_VERSION,
    ).consume()

    session.run(
        """
        MATCH (:Ingredient)-[r:HAS_ALLERGEN]->(:Allergen)
        SET r.presence = coalesce(r.presence, "contains"),
            r.evidence_status = coalesce(r.evidence_status, "inferred"),
            r.sources = CASE
                WHEN r.sources IS NULL OR size(r.sources) = 0 THEN ["legacy"]
                ELSE r.sources
            END,
            r.classification_version = $version
        """,
        version=CLASSIFICATION_VERSION,
    ).consume()


def _clear_generated_suitability(session) -> int:
    record = session.run(
        """
        MATCH (:Ingredient)-[rel:SUITABILITY_FOR]->(:ConsumerGroup)
        WHERE rel.classification_version = $version
        DELETE rel
        RETURN count(rel) AS count
        """,
        version=CLASSIFICATION_VERSION,
    ).single()
    return int(record["count"]) if record else 0


def _materialize_inferred_declarations(session) -> tuple[int, int]:
    """Mirror ingredient allergen evidence into explicit FATO declarations."""

    session.run(
        """
        MATCH (i:Ingredient)
        SET i.canonical_id = coalesce(i.canonical_id, randomUUID())
        """
    ).consume()

    stale_record = session.run(
        """
        MATCH (i:Ingredient)-[:HAS_DECLARATION]->(d:AllergenDeclaration)
              -[:CONCERNS]->(a:Allergen)
        WHERE d.classification_version = $version
          AND d.declaration_type = "inferred_ingredient_presence"
          AND NOT EXISTS {
            MATCH (i)-[:HAS_ALLERGEN]->(a)
          }
        WITH DISTINCT d
        DETACH DELETE d
        RETURN count(*) AS count
        """,
        version=CLASSIFICATION_VERSION,
    ).single()
    stale = int(stale_record["count"]) if stale_record else 0

    record = session.run(
        """
        MATCH (i:Ingredient)-[evidence:HAS_ALLERGEN]->(a:Allergen)
        WITH i, evidence, a,
             "ingredient:" + toString(i.canonical_id)
             + ":allergen:" + a.name
             + ":version:" + $version AS declaration_id
        MERGE (d:AllergenDeclaration {declaration_id: declaration_id})
        ON CREATE SET d.created_at = datetime()
        SET d.declaration_type = "inferred_ingredient_presence",
            d.presence = coalesce(evidence.presence, "contains"),
            d.evidence_status = coalesce(
                evidence.evidence_status, "inferred"
            ),
            d.sources = coalesce(evidence.sources, ["legacy"]),
            d.foodon_ids = coalesce(evidence.foodon_ids, []),
            d.foodon_labels = coalesce(evidence.foodon_labels, []),
            d.keyword_matches = coalesce(evidence.keyword_matches, []),
            d.fato_class_iri = $fato_declaration_class_iri,
            d.classification_version = $version,
            d.updated_at = datetime()
        MERGE (i)-[:HAS_DECLARATION]->(d)
        MERGE (d)-[:CONCERNS]->(a)
        RETURN count(DISTINCT d) AS count
        """,
        fato_declaration_class_iri=FATO_ALLERGEN_DECLARATION_CLASS_IRI,
        version=CLASSIFICATION_VERSION,
    ).single()
    materialized = int(record["count"]) if record else 0
    return stale, materialized


def _candidate_where() -> str:
    return """
    (
      (
        any(allergen IN allergens WHERE allergen IN $blocking_allergens)
        OR any(root IN foodon_roots WHERE root IN $blocking_roots)
        OR size(keyword_hits) > 0
      )
      AND none(pattern IN $exclusion_patterns
               WHERE toLower(coalesce(i.name, "")) =~ pattern)
    )
    """


def _rule_params(group: str) -> dict[str, Any]:
    rule = GROUP_RULES[group]
    return {
        "group": group,
        "blocking_allergens": rule["blocking_allergens"],
        "blocking_roots": rule["blocking_roots"],
        "blocking_keywords": rule["blocking_keywords"],
        "blocking_keyword_regexes": [
            _keyword_regex(keyword) for keyword in rule["blocking_keywords"]
        ],
        "positive_roots": rule["positive_roots"],
        "positive_keywords": rule["positive_keywords"],
        "positive_keyword_regexes": [
            _keyword_regex(keyword) for keyword in rule["positive_keywords"]
        ],
        "positive_exclusion_patterns": POSITIVE_EVIDENCE_EXCLUSIONS,
        "exclusion_patterns": (
            PLANT_ALTERNATIVE_PATTERNS
            if group in {"vegan", "vegetarian"}
            else GLUTEN_SAFE_PATTERNS
            if group == "coeliac"
            else []
        ),
        "version": CLASSIFICATION_VERSION,
    }


def _negative_candidates_query(*, write: bool) -> str:
    tail = """
    RETURN count(DISTINCT i) AS count
    """
    if write:
        tail = """
        MATCH (g:ConsumerGroup {name: $group})
        MERGE (i)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = "not_suitable",
            rel.reason_codes =
              [x IN allergens WHERE x IN $blocking_allergens]
              + [x IN foodon_roots WHERE x IN $blocking_roots]
              + keyword_hits,
            rel.sources =
              ["rules"]
              + CASE
                  WHEN any(root IN foodon_roots
                           WHERE root IN $blocking_roots)
                  THEN ["foodon"] ELSE []
                END
              + CASE
                  WHEN size(allergens) > 0
                  THEN ["allergen_evidence"] ELSE []
                END,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(DISTINCT i) AS count
        """
    return f"""
    MATCH (i:Ingredient)
    OPTIONAL MATCH (i)-[allergen_rel:HAS_ALLERGEN]->(a:Allergen)
    WITH i, collect(DISTINCT {{
      name: a.name,
      sources: coalesce(allergen_rel.sources, [])
    }}) AS allergen_evidence
    WITH i,
         [item IN allergen_evidence
          WHERE item.name IS NOT NULL
            AND any(source IN item.sources
                    WHERE source IN ["foodon", "manual", "label"])
          | item.name] AS allergens
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(:FoodOnClass)-[:SUBCLASS_OF*0..]->(root:FoodOnClass)
    WITH i, allergens, collect(DISTINCT root.foodon_id) AS foodon_roots,
         [idx IN range(0, size($blocking_keywords) - 1)
          WHERE toLower(coalesce(i.name, "")) =~ $blocking_keyword_regexes[idx]
          | $blocking_keywords[idx]] AS keyword_hits
    WHERE {_candidate_where()}
    {tail}
    """


def _positive_candidates_query(*, write: bool) -> str:
    tail = "RETURN count(DISTINCT i) AS count"
    if write:
        tail = """
        MATCH (g:ConsumerGroup {name: $group})
        MERGE (i)-[rel:SUITABILITY_FOR]->(g)
        SET rel.status = "suitable",
            rel.reason_codes = CASE
              WHEN size(positive_roots) > 0 THEN positive_roots
              ELSE ["explicit_suitability_term"] END,
            rel.sources = CASE
              WHEN size(positive_roots) > 0 THEN ["foodon", "rules"]
              ELSE ["rules"] END,
            rel.classification_version = $version,
            rel.updated_at = datetime()
        RETURN count(DISTINCT i) AS count
        """
    return f"""
    MATCH (i:Ingredient)
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(:FoodOnClass)-[:SUBCLASS_OF*0..]->(root:FoodOnClass)
    WITH i,
         [value IN collect(DISTINCT root.foodon_id)
          WHERE value IN $positive_roots] AS positive_roots,
         [idx IN range(0, size($positive_keywords) - 1)
          WHERE toLower(coalesce(i.name, "")) =~ $positive_keyword_regexes[idx]
          | $positive_keywords[idx]] AS keyword_hits
    WHERE (size(positive_roots) > 0 OR size(keyword_hits) > 0)
      AND none(pattern IN $positive_exclusion_patterns
               WHERE toLower(coalesce(i.name, "")) =~ pattern)
      AND NOT EXISTS {{
        MATCH (i)-[existing:SUITABILITY_FOR]->(:ConsumerGroup {{name: $group}})
        WHERE existing.status = "not_suitable"
      }}
    {tail}
    """


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add selected FATO/FoodOn semantics to the existing graph."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the command is read-only.",
    )
    args = parser.parse_args()

    graph = _driver()
    try:
        with graph.session() as session:
            ingredient_count = _scalar(
                session, "MATCH (i:Ingredient) RETURN count(i) AS count"
            )
            allergen_edge_count = _scalar(
                session,
                "MATCH (:Ingredient)-[r:HAS_ALLERGEN]->(:Allergen) "
                "RETURN count(r) AS count",
            )
            print(
                f"ingredients={ingredient_count} "
                f"allergen_edges={allergen_edge_count} "
                f"mode={'apply' if args.apply else 'dry-run'}"
            )

            if args.apply:
                _ensure_schema(session)
                _upsert_ontology_crosswalk(session)
                stale, declarations = _materialize_inferred_declarations(
                    session
                )
                print(
                    f"declarations={declarations} "
                    f"stale_declarations_removed={stale}"
                )
                cleared = _clear_generated_suitability(session)
                print(f"cleared_previous_suitability={cleared}")

            # Only these three groups have composition rules. The remaining
            # FATO vocabulary nodes are created above but deliberately stay
            # unknown until certification/profile evidence exists.
            for group in GROUP_RULES:
                params = _rule_params(group)
                negative = _scalar(
                    session,
                    _negative_candidates_query(write=args.apply),
                    **params,
                )
                positive = _scalar(
                    session,
                    _positive_candidates_query(write=args.apply),
                    **params,
                )
                print(
                    f"{group}: not_suitable={negative} suitable={positive}"
                )
    finally:
        graph.close()


if __name__ == "__main__":
    main()
