#!/usr/bin/env python3
"""Tag recipes with Nutri-Score A as healthy_and_nutritious in Neo4j.

Source priority (highest first): safefood_rcsi, safefood_web, planeat,
slovenian, recipe1m_original, eu, usda, irish, hungarian, safefood.
One Nutri-Score picked per recipe_id.
"""
from __future__ import annotations

import os
import subprocess
from neo4j import GraphDatabase

NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")
BATCH = 500

SQL = """
WITH ranked AS (
  SELECT recipe_id,
         nutri_score->>'nutri_score' AS grade,
         ROW_NUMBER() OVER (
           PARTITION BY recipe_id
           ORDER BY CASE nutrition_source
             WHEN 'safefood_rcsi'     THEN 1
             WHEN 'safefood_web'      THEN 2
             WHEN 'planeat'           THEN 3
             WHEN 'slovenian'         THEN 4
             WHEN 'recipe1m_original' THEN 5
             WHEN 'eu'                THEN 6
             WHEN 'usda'              THEN 7
             WHEN 'irish'             THEN 8
             WHEN 'hungarian'         THEN 9
             ELSE 10 END
         ) AS rn
  FROM "nutrients-recipe-profiles"
  WHERE nutri_score IS NOT NULL
    AND nutri_score->>'nutri_score' IS NOT NULL
    AND nutri_score->>'nutri_score' != ''
)
SELECT recipe_id FROM ranked
WHERE rn = 1 AND grade = 'Nutriscore_A';
"""


def pg_fetch_a_recipes() -> list[str]:
    cmd = ["docker", "exec", "wisefood-postgres",
           "psql", "-U", "postgres", "-d", "nutrients",
           "-t", "-A", "-c", SQL]
    out = subprocess.check_output(cmd).decode(errors="replace")
    return [line.strip() for line in out.splitlines() if line.strip()]


def apply_tag(driver, recipe_ids: list[str]):
    tag_name = "healthy_and_nutritious"
    with driver.session() as s:
        s.run("MERGE (:Tag {name: $n})", n=tag_name)
    for i in range(0, len(recipe_ids), BATCH):
        batch = recipe_ids[i:i + BATCH]
        with driver.session() as s:
            s.run("""
UNWIND $ids AS rid
MATCH (r:Recipe {recipe_id: rid})
MERGE (t:Tag {name: 'healthy_and_nutritious'})
MERGE (r)-[:HAS_TAG]->(t)
""", ids=batch)
        if (i // BATCH) % 10 == 0:
            print(f"  {min(i + BATCH, len(recipe_ids)):,} / {len(recipe_ids):,}", flush=True)


def main():
    print("Fetching Nutri-Score A recipes from Postgres...", flush=True)
    ids = pg_fetch_a_recipes()
    print(f"  {len(ids):,} recipes with Nutri-Score A")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    print("Applying healthy_and_nutritious tag to Neo4j...", flush=True)
    apply_tag(driver, ids)
    driver.close()

    print(f"Done — {len(ids):,} recipes tagged as healthy_and_nutritious.")


if __name__ == "__main__":
    main()
