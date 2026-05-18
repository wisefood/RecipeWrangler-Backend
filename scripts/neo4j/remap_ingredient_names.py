#!/usr/bin/env python3
"""One-shot remap for a small hand-curated set of ingredient names.

After the bulk normalize pass a handful of cases survive that pattern-based
rules can't fix:

- USDA "category-first" inversions: ``oil vegetable`` → ``vegetable oil``,
  ``juice fruit`` → ``fruit juice``.
- Plural false positives from words ending in -us: ``convolvulu`` →
  ``water convolvulus`` (since my generic plural rule wrongly chomped the s).
- Suppressed qualifiers: ``sauce ready-to-serve`` → ``sauce``,
  ``spices paprika`` → ``paprika``.

Idempotent: re-running with the same map is a no-op once applied.
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

REMAP: dict[str, str] = {
    # USDA "category first" inversions
    "oil vegetable": "vegetable oil",
    "juice fruit": "fruit juice",
    "juice lemon": "lemon juice",
    "juice orange": "orange juice",
    "juice lime": "lime juice",
    "juice apple": "apple juice",
    "juice tomato": "tomato juice",
    "soup chicken": "chicken soup",
    "soup beef": "beef soup",
    "soup tomato": "tomato soup",
    "butter powder": "powdered butter",
    "yogurt plain": "plain yogurt",
    "yogurt vanilla": "vanilla yogurt",
    "yogurt fruit": "fruit yogurt",
    "milk skim": "skim milk",
    "milk whole": "whole milk",
    "milk soy": "soy milk",
    "tomato cherry": "cherry tomato",
    "tomato grape": "grape tomato",
    "cheese cheddar": "cheddar cheese",
    "cheese mozzarella": "mozzarella cheese",
    "cheese parmesan": "parmesan cheese",
    "bread white": "white bread",
    "bread wheat": "wheat bread",
    # Drop the qualifier
    "sauce ready-to-serve": "sauce",
    "spices basil": "basil",
    "spices paprika": "paprika",
    "spices cumin": "cumin",
    "spices oregano": "oregano",
    "spices thyme": "thyme",
    "spices rosemary": "rosemary",
    "spices cinnamon": "cinnamon",
    "spices nutmeg": "nutmeg",
    "spices ginger": "ginger",
    "spices coriander": "coriander",
    "spices pepper black": "black pepper",
    "spices pepper red": "red pepper",
    "spices pepper white": "white pepper",
    "spice basil": "basil",
    "spice paprika": "paprika",
    # Plural false positives (-us words)
    "water convolvulu": "water convolvulus",
    "asparagu": "asparagus",
    "octopu": "octopus",
    "cactu": "cactus",
    "fungu": "fungus",
    "hummu": "hummus",
    "couscou": "couscous",
    "molasse": "molasses",
    # Compound noise that should just collapse
    "salt pepper": "salt",  # appears as standalone label from the carbon dataset
    "garlic vinegar": "vinegar",
    "tomato onion": "tomato",
    "no salt tomato": "tomato",
}

REMAP_CYPHER = """
UNWIND $rows AS row
CALL (row) {
    MATCH (old:Ingredient {name: row.raw})
    WITH old, row LIMIT 1
    MERGE (clean:Ingredient {name: row.clean})
        ON CREATE SET clean.canonical_id = randomUUID()
    WITH old, clean
    MATCH (rec:Recipe)-[h:HAS_INGREDIENT]->(old)
    WITH old, clean, rec, h, properties(h) AS props
    CREATE (rec)-[h2:HAS_INGREDIENT]->(clean)
    SET h2 = props
    DELETE h
} IN TRANSACTIONS OF 200 ROWS
"""

DROP_ORPHANS = """
MATCH (i:Ingredient)
WHERE NOT (:Recipe)-[:HAS_INGREDIENT]->(i)
  AND NOT (i)<-[:HAS_INGREDIENT_ORIGINAL]-()
  AND NOT (i)-[:HAS_ALLERGEN]-()
  AND NOT (i)-[:HAS_CLASS]-()
  AND NOT (i)-[:HAS_SUBSTITUTION]-()
  AND NOT (i)-[:FLAVORDB_EQUIVALENT]-()
WITH i LIMIT 5000
DETACH DELETE i
RETURN count(*) AS deleted
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    drv = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    try:
        with drv.session() as s:
            # check which raw names actually exist
            rows: list[dict] = []
            for raw, clean in REMAP.items():
                res = s.run(
                    "MATCH (i:Ingredient {name: $n}) "
                    "OPTIONAL MATCH (rec:Recipe)-[:HAS_INGREDIENT]->(i) "
                    "RETURN count(DISTINCT rec) AS uses",
                    n=raw,
                ).single()
                if res and res["uses"] > 0:
                    rows.append({"raw": raw, "clean": clean})
                    print(f"  {raw!r:35s} -> {clean!r:30s} ({res['uses']} uses)")
            print(f"\n{len(rows)}/{len(REMAP)} remap entries present in graph")

            if not args.write:
                print("[dry-run] re-run with --write to apply.")
                return 0

            for i in range(0, len(rows), 50):
                s.run(REMAP_CYPHER, rows=rows[i : i + 50]).consume()
            print("renames applied.")
            total = 0
            while True:
                rec = s.run(DROP_ORPHANS).single()
                d = rec["deleted"] if rec else 0
                total += d
                if not d:
                    break
            print(f"orphans deleted: {total}")
    finally:
        drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
