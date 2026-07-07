#!/usr/bin/env python3
"""Repair verified recipe-to-canonical-ingredient mapping errors in Neo4j."""

from __future__ import annotations

import os
from pathlib import Path

from neo4j import GraphDatabase

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[1]

# Verified against the HealthyFoods source ingredient lines. Both recipes use
# ground chipotle powder; the canonical mapping incorrectly selected peanut
# powder, producing false peanut-allergen edges.
REPAIRS = [
    {
        "source": "HealthyFoods",
        "recipe_id": "1712744020",
        "old_name": "peanut powder",
        "new_name": "chipotle powder",
    },
    {
        "source": "HealthyFoods",
        "recipe_id": "9729874174",
        "old_name": "peanut powder",
        "new_name": "chipotle powder",
    },
]


def main() -> None:
    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j"))
    password = os.getenv("NEO4J_PASSWORD")
    driver = GraphDatabase.driver(uri, auth=(username, password))
    query = """
    UNWIND $repairs AS repair
    MATCH (r:Recipe {source: repair.source})-[old_rel:HAS_INGREDIENT]->
          (old:Ingredient {name: repair.old_name})
    WHERE toString(r.recipe_id) = repair.recipe_id
    MERGE (replacement:Ingredient {name: repair.new_name})
    ON CREATE SET replacement.canonical_id = randomUUID(),
                  replacement.source = repair.source,
                  replacement.status = "resolved"
    MERGE (r)-[new_rel:HAS_INGREDIENT]->(replacement)
    SET new_rel = properties(old_rel)
    DELETE old_rel
    RETURN count(*) AS repaired
    """
    try:
        with driver.session() as session:
            repaired = int(session.run(query, repairs=REPAIRS).single()["repaired"])
    finally:
        driver.close()
    print(f"Repaired {repaired} recipe ingredient mappings")


if __name__ == "__main__":
    main()
