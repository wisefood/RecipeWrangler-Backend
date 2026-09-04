import argparse
import os
import re
from typing import Optional
from pathlib import Path

from neo4j import GraphDatabase
from recipe_wrangler.utils.food_ontology import (
    ALLERGEN_DETECTION_RULES as ALLERGENS,
    ALLERGEN_EXCLUSION_REGEXES,
    ALLERGEN_ONTOLOGY_MAPPINGS,
    CLASSIFICATION_VERSION,
    FATO_ALLERGEN_CLASS_IRI,
    FATO_ALLERGEN_DECLARATION_CLASS_IRI,
    GLUTEN_SAFE_REGEXES,
    MILK_PLANT_EXCLUSION_REGEXES,
)
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None


# Purpose: Tag ingredients with allergen evidence (FoodOn ancestry + keyword fallback).



def _keyword_regex(keyword: str) -> str:
    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"\s+")
    return rf".*\b{escaped}(e?s)?\b.*"


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool):
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or use --no-auth if allowed."
        )
    return GraphDatabase.driver(uri, auth=(username, password))


def _ensure_constraints(driver) -> None:
    statements = [
        (
            "CREATE CONSTRAINT allergen_name IF NOT EXISTS "
            "FOR (n:Allergen) REQUIRE n.name IS UNIQUE"
        ),
        (
            "CREATE CONSTRAINT allergen_declaration_id IF NOT EXISTS "
            "FOR (n:AllergenDeclaration) REQUIRE n.declaration_id IS UNIQUE"
        ),
    ]
    with driver.session() as session:
        for statement in statements:
            session.run(statement).consume()


def _ontology_params(allergen_name: str) -> dict[str, str | None]:
    mapping = ALLERGEN_ONTOLOGY_MAPPINGS.get(allergen_name)
    return {
        "fato_class_iri": FATO_ALLERGEN_CLASS_IRI,
        "fato_declaration_class_iri": (
            FATO_ALLERGEN_DECLARATION_CLASS_IRI
        ),
        "foodon_label_claim_id": (
            mapping.foodon_label_claim_id if mapping else None
        ),
        "eu_label": mapping.eu_label if mapping else allergen_name,
        "classification_version": CLASSIFICATION_VERSION,
    }


def _tag_by_foodon(driver, allergen_name: str, roots: list[str]) -> int:
    query = """
    MATCH (i:Ingredient)-[:HAS_CLASS]->(f:FoodOnClass)
    MATCH (f)-[:SUBCLASS_OF*0..]->(a:FoodOnClass)
    WHERE a.foodon_id IN $roots
      AND none(pattern IN $name_exclusions
               WHERE toLower(i.name) =~ pattern)
      AND (
        $allergen_name <> 'milk'
        OR none(pattern IN $milk_exclusions
                WHERE toLower(i.name) =~ pattern)
      )
    WITH i,
         collect(distinct a.foodon_id) AS foodon_ids,
         collect(distinct a.label) AS foodon_labels
    MERGE (al:Allergen {name: $allergen_name})
    SET al.canonical_id = $allergen_name,
        al.eu_label = $eu_label,
        al.jurisdiction = "EU",
        al.fato_class_iri = $fato_class_iri,
        al.foodon_label_claim_id = $foodon_label_claim_id,
        al.classification_version = $classification_version
    MERGE (i)-[r:HAS_ALLERGEN]->(al)
    SET r.sources = CASE
            WHEN r.sources IS NULL THEN ["foodon"]
            WHEN "foodon" IN r.sources THEN r.sources
            ELSE r.sources + ["foodon"]
        END,
        r.foodon_ids = foodon_ids,
        r.foodon_labels = foodon_labels,
        r.presence = "contains",
        r.evidence_status = "inferred",
        r.classification_version = $classification_version
    WITH i, al, r
    SET i.canonical_id = coalesce(i.canonical_id, randomUUID())
    MERGE (declaration:AllergenDeclaration {
        declaration_id:
            "ingredient:" + toString(i.canonical_id)
            + ":allergen:" + al.name
            + ":version:" + $classification_version
    })
    ON CREATE SET declaration.created_at = datetime()
    SET declaration.declaration_type = "inferred_ingredient_presence",
        declaration.presence = r.presence,
        declaration.evidence_status = r.evidence_status,
        declaration.sources = r.sources,
        declaration.foodon_ids = r.foodon_ids,
        declaration.foodon_labels = r.foodon_labels,
        declaration.keyword_matches = coalesce(r.keyword_matches, []),
        declaration.fato_class_iri = $fato_declaration_class_iri,
        declaration.classification_version = $classification_version,
        declaration.updated_at = datetime()
    MERGE (i)-[:HAS_DECLARATION]->(declaration)
    MERGE (declaration)-[:CONCERNS]->(al)
    RETURN count(distinct i) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            allergen_name=allergen_name,
            roots=roots,
            milk_exclusions=MILK_PLANT_EXCLUSION_REGEXES,
            name_exclusions=ALLERGEN_EXCLUSION_REGEXES.get(
                allergen_name, []
            ),
            **_ontology_params(allergen_name),
        )
        return int(result.single()["tagged"])


def _tag_by_keyword(driver, allergen_name: str, keywords: list[str]) -> int:
    keywords = [k.strip().casefold() for k in keywords if k.strip()]
    keyword_regexes = [_keyword_regex(keyword) for keyword in keywords]
    query = """
    MATCH (i:Ingredient)
    WHERE i.name IS NOT NULL
      AND (
        ($allergen_name IN ['milk', 'gluten', 'wheat']
         AND any(pattern IN $keyword_regexes
                 WHERE toLower(i.name) =~ pattern))
        OR
        (NOT $allergen_name IN ['milk', 'gluten', 'wheat']
         AND any(pattern IN $keyword_regexes
                 WHERE toLower(i.name) =~ pattern))
      )
      AND none(pattern IN $name_exclusions
               WHERE toLower(i.name) =~ pattern)
      AND (
        $allergen_name <> 'milk'
        OR none(pattern IN $milk_exclusions
                WHERE toLower(i.name) =~ pattern)
      )
    WITH i, [idx IN range(0, size($keywords) - 1)
             WHERE (
               ($allergen_name IN ['milk', 'gluten', 'wheat']
                AND toLower(i.name) =~ $keyword_regexes[idx])
               OR
               (NOT $allergen_name IN ['milk', 'gluten', 'wheat']
                AND toLower(i.name) =~ $keyword_regexes[idx])
             ) |
             $keywords[idx]] AS hits
    MERGE (al:Allergen {name: $allergen_name})
    SET al.canonical_id = $allergen_name,
        al.eu_label = $eu_label,
        al.jurisdiction = "EU",
        al.fato_class_iri = $fato_class_iri,
        al.foodon_label_claim_id = $foodon_label_claim_id,
        al.classification_version = $classification_version
    MERGE (i)-[r:HAS_ALLERGEN]->(al)
    SET r.sources = CASE
            WHEN r.sources IS NULL THEN ["keyword"]
            WHEN "keyword" IN r.sources THEN r.sources
            ELSE r.sources + ["keyword"]
        END,
        r.keyword_matches = CASE
            WHEN r.keyword_matches IS NULL THEN hits
            ELSE r.keyword_matches + [x IN hits WHERE NOT x IN r.keyword_matches]
        END,
        r.presence = "contains",
        r.evidence_status = "inferred",
        r.classification_version = $classification_version
    WITH i, al, r
    SET i.canonical_id = coalesce(i.canonical_id, randomUUID())
    MERGE (declaration:AllergenDeclaration {
        declaration_id:
            "ingredient:" + toString(i.canonical_id)
            + ":allergen:" + al.name
            + ":version:" + $classification_version
    })
    ON CREATE SET declaration.created_at = datetime()
    SET declaration.declaration_type = "inferred_ingredient_presence",
        declaration.presence = r.presence,
        declaration.evidence_status = r.evidence_status,
        declaration.sources = r.sources,
        declaration.foodon_ids = coalesce(r.foodon_ids, []),
        declaration.foodon_labels = coalesce(r.foodon_labels, []),
        declaration.keyword_matches = r.keyword_matches,
        declaration.fato_class_iri = $fato_declaration_class_iri,
        declaration.classification_version = $classification_version,
        declaration.updated_at = datetime()
    MERGE (i)-[:HAS_DECLARATION]->(declaration)
    MERGE (declaration)-[:CONCERNS]->(al)
    RETURN count(distinct i) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            allergen_name=allergen_name,
            keywords=keywords,
            keyword_regexes=keyword_regexes,
            milk_exclusions=MILK_PLANT_EXCLUSION_REGEXES,
            name_exclusions=ALLERGEN_EXCLUSION_REGEXES.get(
                allergen_name, []
            ),
            **_ontology_params(allergen_name),
        )
        return int(result.single()["tagged"])


def _clear_allergen_edges(driver, allergen_names: set[str]) -> tuple[int, int]:
    """Remove selected evidence and its inferred declarations atomically."""
    names = sorted(allergen_names)

    def clear(tx) -> tuple[int, int]:
        declarations = tx.run(
            """
            MATCH (i:Ingredient)-[:HAS_DECLARATION]->
                  (d:AllergenDeclaration)-[:CONCERNS]->(a:Allergen)
            WHERE a.name IN $allergen_names
              AND d.declaration_type = "inferred_ingredient_presence"
            WITH DISTINCT d
            DETACH DELETE d
            RETURN count(*) AS deleted
            """,
            allergen_names=names,
        ).single()
        edges = tx.run(
            """
            MATCH (:Ingredient)-[r:HAS_ALLERGEN]->(a:Allergen)
            WHERE a.name IN $allergen_names
            DELETE r
            RETURN count(r) AS deleted
            """,
            allergen_names=names,
        ).single()
        return (
            int(edges["deleted"]) if edges else 0,
            int(declarations["deleted"]) if declarations else 0,
        )

    with driver.session() as session:
        return session.execute_write(clear)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tag Neo4j ingredients with supported allergen groups."
    )
    parser.add_argument(
        "--allergens",
        nargs="+",
        choices=sorted(ALLERGENS),
        help="Only backfill the selected allergen labels (default: all).",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Delete existing HAS_ALLERGEN edges for the selected allergens "
            "before rebuilding them."
        ),
    )
    args = parser.parse_args()

    if load_dotenv:
        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / ".env")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"

    driver = _connect(uri, username, password, no_auth)
    try:
        _ensure_constraints(driver)
        selected = set(args.allergens or ALLERGENS)
        if args.replace:
            deleted, declarations = _clear_allergen_edges(driver, selected)
            print(f"deleted existing edges: {deleted}")
            print(f"deleted matching inferred declarations: {declarations}")
        items = [
            (name, config)
            for name, config in ALLERGENS.items()
            if name in selected
        ]
        iterator = tqdm(items, desc="Tagging allergens") if tqdm else items
        for allergen_name, config in iterator:
            foodon_tagged = _tag_by_foodon(
                driver, allergen_name, config["roots"]
            )
            keyword_tagged = _tag_by_keyword(
                driver, allergen_name, config["keywords"]
            )
            print(f"{allergen_name}: foodon={foodon_tagged}, keyword={keyword_tagged}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
