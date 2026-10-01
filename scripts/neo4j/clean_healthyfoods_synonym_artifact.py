#!/usr/bin/env python3
"""Rename HealthyFoods Ingredient nodes contaminated by a site scraping artifact.

healthyfood.com renders an inline regional-name tooltip (e.g. "corn
(sweetcorn)") whose trigger element leaks into scraped text as a bare "X"
token, sometimes with an orphaned "s" from a stripped "(s)" plural marker
next to it (e.g. "eggplant aubergine X s" -> "eggplant aubergine"). That
artifact was ingested verbatim into Neo4j Ingredient node names, giving them
no HAS_SUBSTITUTION edges of their own and starving substitution search.

This renames every affected node to its cleaned form (merging into an
existing clean node when one already exists), following the same
rename-and-rewire pattern as the deleted remap_ingredient_names.py: MERGE the
clean node, copy each HAS_INGREDIENT edge with its original properties,
delete the old edge, then drop any Ingredient left with zero relationships.

Idempotent: re-running after --write finds nothing left to rename.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from neo4j import GraphDatabase  # noqa: E402


# Same rule as scripts/import/healthyfoods/transform_healthyfood_recipes_json.py's
# _strip_synonym_tooltip_artifact — kept in sync by hand since that script isn't
# an importable module. A standalone "s" never appears in HealthyFoods text
# without the "X" artifact nearby (verified against the raw source), so both
# are safe to strip together.
_STANDALONE_X_RE = re.compile(r"(?<!\S)[Xx](?!\S)")
_STANDALONE_S_RE = re.compile(r"(?<!\S)s(?!\S)")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?")


def _strip_synonym_tooltip_artifact(text: str) -> str:
    def repl(match: re.Match) -> str:
        # Leave a real "2 x 400g" multiplier alone -- only the tooltip
        # artifact (an X with no number on both sides) gets stripped.
        before = text[: match.start()].rstrip()
        after = text[match.end() :].lstrip()
        before_tok = before.rsplit(None, 1)[-1] if before else ""
        if _NUMBER_RE.fullmatch(before_tok) and _NUMBER_RE.match(after):
            return match.group(0)
        return " "

    cleaned = _STANDALONE_X_RE.sub(repl, text)
    # Orphaned "(s)" plural marker left behind by the same site bug -- only
    # ever appears in HealthyFoods text alongside the X artifact (verified),
    # so only strip it once an X was actually stripped from this string.
    if cleaned != text:
        cleaned = _STANDALONE_S_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,")


RENAME_CYPHER = """
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

# Husk nodes left behind by RENAME_CYPHER: the old messy name, no longer
# linked to any recipe, but kept alive by a HAS_CLASS/other edge so
# DROP_ORPHANS (which requires *zero* relationships) won't touch them.
# Scoped strictly to the artifact pattern -- never a generic sweep.
DROP_RENAMED_HUSKS = """
MATCH (i:Ingredient)
WHERE i.name =~ '(?i).*(^| )x( |$).*'
  AND NOT (:Recipe)-[:HAS_INGREDIENT]->(i)
WITH i LIMIT 5000
DETACH DELETE i
RETURN count(*) AS deleted
"""

UPDATE_ORIGINAL_TEXT_CYPHER = """
UNWIND $rows AS row
CALL (row) {
    MATCH (o:Ingredients_original {original_id: row.oid})
    SET o.name = row.name, o.original_text = row.text
} IN TRANSACTIONS OF 200 ROWS
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
            # Step 1: canonical Ingredient nodes (substitution-graph keys).
            names = [
                r["name"]
                for r in s.run(
                    "MATCH (r:Recipe {source: 'HealthyFoods'})-[:HAS_INGREDIENT]->(i:Ingredient) "
                    "RETURN DISTINCT i.name AS name"
                )
            ]
            ingredient_rows: list[dict] = []
            for name in names:
                clean = _strip_synonym_tooltip_artifact(name)
                if clean and clean != name:
                    ingredient_rows.append({"raw": name, "clean": clean})

            print(f"Step 1 — {len(ingredient_rows)} Ingredient nodes to rename:")
            for row in sorted(ingredient_rows, key=lambda r: r["raw"])[:10]:
                print(f"  {row['raw']!r:55s} -> {row['clean']!r}")
            if len(ingredient_rows) > 10:
                print(f"  ... and {len(ingredient_rows) - 10} more")

            # Step 2: Ingredients_original display nodes (raw recipe text
            # shown in the UI) -- unique per (recipe, position), so a direct
            # property update, no MERGE/rewire needed.
            original_rows_raw = list(
                s.run(
                    "MATCH (r:Recipe {source: 'HealthyFoods'})-[:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original) "
                    "RETURN DISTINCT o.original_id AS oid, o.name AS name, o.original_text AS text"
                )
            )
            original_rows: list[dict] = []
            for r in original_rows_raw:
                name, text = r["name"] or "", r["text"] or ""
                clean_name = _strip_synonym_tooltip_artifact(name)
                clean_text = _strip_synonym_tooltip_artifact(text)
                if (clean_name and clean_name != name) or (clean_text and clean_text != text):
                    original_rows.append({
                        "oid": r["oid"],
                        "name": clean_name or name,
                        "text": clean_text or text,
                    })

            print(f"\nStep 2 — {len(original_rows)} Ingredients_original display nodes to update:")
            for row in original_rows[:10]:
                print(f"  {row['oid']} -> {row['name']!r}")
            if len(original_rows) > 10:
                print(f"  ... and {len(original_rows) - 10} more")

            if not args.write:
                print("\n[dry-run] re-run with --write to apply.")
                return 0

            for i in range(0, len(ingredient_rows), 50):
                s.run(RENAME_CYPHER, rows=ingredient_rows[i : i + 50]).consume()
            print("\nStep 1 applied.")

            husks = 0
            while True:
                rec = s.run(DROP_RENAMED_HUSKS).single()
                d = rec["deleted"] if rec else 0
                husks += d
                if not d:
                    break
            print(f"dropped {husks} renamed-away husk nodes.")

            for i in range(0, len(original_rows), 200):
                s.run(UPDATE_ORIGINAL_TEXT_CYPHER, rows=original_rows[i : i + 200]).consume()
            print("Step 2 applied.")
    finally:
        drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
