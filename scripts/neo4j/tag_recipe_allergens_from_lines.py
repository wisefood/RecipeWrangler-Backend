"""Add recipe-level allergen evidence read from the original ingredient lines.

Stored ingredient names are reduced ("soy milk" -> "milk"), so allergens such as soy can be lost.
This reads ``Ingredients_original`` text with the shared name detector and ADDS a recipe-level
declaration + ``HAS_ALLERGEN`` edge for each definite allergen the graph does not already carry.
It never removes or changes existing evidence. Dry-run unless ``--apply``; every written node and
edge carries ``classification_version = 'original-line-v1'`` so ``--rollback`` is one query.

    PYTHONPATH=src python scripts/neo4j/tag_recipe_allergens_from_lines.py --plan plan.json
    PYTHONPATH=src python scripts/neo4j/tag_recipe_allergens_from_lines.py --plan plan.json --apply
    PYTHONPATH=src python scripts/neo4j/tag_recipe_allergens_from_lines.py --rollback
"""
from __future__ import annotations

import argparse
import json
import os
from multiprocessing import Pool
from pathlib import Path

from dotenv import dotenv_values
from neo4j import GraphDatabase

from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names as detect
from recipe_wrangler.utils.line_allergens import is_heading

VERSION = "original-line-v1"
SOURCE = "original_line_keywords"

READ = """MATCH (r:Recipe)
CALL (r) { OPTIONAL MATCH (r)-[:HAS_INGREDIENT_ORIGINAL]->(o) RETURN collect(DISTINCT o.original_text) AS lines }
CALL (r) { OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i) RETURN collect(DISTINCT i.name) AS names }
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
  WITH collect(DISTINCT a.name) AS via_ingredients
  OPTIONAL MATCH (r)-[:HAS_ALLERGEN]->(b:Allergen)
  WITH via_ingredients, collect(DISTINCT b.name) AS direct
  RETURN via_ingredients + direct AS graph
}
RETURN r.recipe_id AS id, r.source AS source, r.title AS title, lines, names, graph"""

WRITE = """UNWIND $items AS item
MATCH (r:Recipe {recipe_id: item.id})
MATCH (a:Allergen {name: item.allergen})
MERGE (d:AllergenDeclaration {declaration_id: 'recipe:' + toString(item.id) + ':line:' + item.allergen})
  ON CREATE SET d.created_at = datetime(), d.declaration_type = 'inferred_recipe_line_presence',
    d.presence = 'contains', d.evidence_status = 'inferred', d.sources = [$source],
    d.keyword_matches = item.lines, d.classification_version = $version
MERGE (r)-[:HAS_DECLARATION]->(d)
MERGE (d)-[:CONCERNS]->(a)
MERGE (r)-[h:HAS_ALLERGEN]->(a)
  ON CREATE SET h.sources = [$source], h.presence = 'contains', h.evidence_status = 'inferred',
    h.keyword_matches = item.lines, h.classification_version = $version
SET r.projection_pending = true
RETURN count(DISTINCT r) AS recipes, count(d) AS declarations"""

ROLLBACK = """MATCH (r:Recipe)-[h:HAS_ALLERGEN]->(:Allergen) WHERE h.classification_version = $version AND $source IN h.sources
WITH collect(DISTINCT r) AS recipes, collect(h) AS edges
FOREACH (h IN edges | DELETE h)
WITH recipes
MATCH (d:AllergenDeclaration) WHERE d.classification_version = $version AND $source IN d.sources
WITH recipes, collect(d) AS declarations
FOREACH (r IN recipes | SET r.projection_pending = true)
FOREACH (d IN declarations | DETACH DELETE d)
RETURN size(recipes) AS recipes, size(declarations) AS declarations"""


def _one(text: str) -> tuple[str, list[tuple[str, bool]]]:
    return text, [(e["allergen"], e["presence"] == "contains") for e in detect([text])]


def _driver():
    cfg = {**dotenv_values(Path(__file__).resolve().parents[2] / ".env"), **os.environ}
    return GraphDatabase.driver(
        cfg.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(cfg.get("NEO4J_USER", "neo4j"), cfg.get("NEO4J_PASSWORD", "password123")),
    )


def build_plan(rows: list[dict], cache: dict) -> list[dict]:
    """Definite allergens present in the original lines and absent from every stored signal."""
    plan = []
    for row in rows:
        lines = [t for t in row["lines"] if t and not is_heading(t)]
        if not lines:
            continue
        found: dict[str, list[str]] = {}
        for line in lines:
            for allergen, contains in cache.get(line, []):
                if contains:
                    found.setdefault(allergen, []).append(line.strip()[:160])
        have = set(row["graph"])
        for name in row["names"]:
            have |= {a for a, c in cache.get(name, []) if c}
        for allergen, evidence in sorted(found.items()):
            if allergen not in have:
                plan.append({"id": row["id"], "source": row["source"], "title": row["title"],
                             "allergen": allergen, "lines": evidence[:5]})
    return plan


def load_plan() -> list[dict]:
    with _driver().session(default_access_mode="READ") as session:
        rows = [dict(r) for r in session.run(READ)]
    texts = sorted({t for r in rows for t in (r["lines"] or []) + (r["names"] or []) if t})
    with Pool(6) as pool:
        cache = dict(pool.map(_one, texts, chunksize=200))
    return build_plan(rows, cache)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", help="write the planned additions to this JSON file")
    parser.add_argument("--apply", action="store_true", help="write the plan to Neo4j (default: dry-run)")
    parser.add_argument("--rollback", action="store_true", help="remove everything this tool wrote")
    args = parser.parse_args()
    if args.rollback:
        with _driver().session() as session:
            print(dict(session.run(ROLLBACK, version=VERSION, source=SOURCE).single()))
        return
    plan = load_plan()
    if args.plan:
        Path(args.plan).write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    recipes = len({p["id"] for p in plan})
    print(f"{len(plan)} additions across {recipes} recipes; mode={'apply' if args.apply else 'dry-run'}")
    if not args.apply:
        return
    with _driver().session() as session:
        for start in range(0, len(plan), 100):
            session.run(WRITE, items=plan[start:start + 100], version=VERSION, source=SOURCE).consume()
    print("written")


if __name__ == "__main__":
    main()
