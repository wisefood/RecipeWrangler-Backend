#!/usr/bin/env python3
"""Normalize and dedupe Ingredient names across migrated + recipe1m sources.

Runs a deterministic Python-side cleanup over every (raw_name, recipe_count)
pair touched by HealthyFoods / MyPlate / Irish_SafeFood / recipe1m recipes,
computes a canonical clean_name, and rewrites HAS_INGREDIENT rels to point to
the deduplicated clean node. FoodHero is skipped — its strings are too broken
for a regex pass.

Cleanups applied, in order:

1. Strip trailing ``" X"`` marker (carbon-foundation dataset suffix).
2. Strip parenthetical clauses ``(...)``.
3. Strip leading quantity (``\\d+(\\.\\d+)?(/\\d+)?``) + optional unit.
4. Strip leading articles (``a / an / the``).
5. If the string contains commas, USDA-style cleanup:
   - if the first segment is a "category" word (``spices``, ``sauce``,
     ``vegetables``, etc.) take the second segment; otherwise take the first.
6. Lowercase, collapse internal whitespace, strip leading/trailing punctuation.
7. Singular ↔ plural collapse for the easy cases (``onions → onion``,
   ``eggs → egg``).
8. Drop rows whose clean name is empty or a known noise token.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/neo4j/normalize_ingredient_names.py        # dry-run
    PYTHONPATH=src .venv/bin/python scripts/neo4j/normalize_ingredient_names.py --write
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from neo4j import GraphDatabase  # noqa: E402

DEFAULT_SOURCES = ("HealthyFoods", "MyPlate", "Curated Irish Recipes", "recipe1m")

USDA_CATEGORY_WORDS = {
    "spices", "spice",
    "sauce", "sauces",
    "vegetables", "vegetable",
    "fruits", "fruit",
    "juices", "juice",
    "oils", "oil",
    "vinegars", "vinegar",
    "syrups", "syrup",
    "babyfood",
    "fish",
    "beef",
    "pork",
    "poultry",
    "chicken",
    "lamb",
    "cheese",
    "yogurt",
    "milk",
    "cereals", "cereal",
    "bread", "breads",
    "snacks",
    "soup", "soups",
    "candies",
    "nuts",
    "seeds",
    "beverages",
    "leavening agents",
    "fast foods",
}

NOISE_TOKENS = {
    "", "to", "and", "or", "of", "with", "for", "in", "by",
    "fresh", "frozen", "canned", "cooked", "raw", "dried", "ground",
    "chopped", "sliced", "diced", "mixed",
    "x", "or canned", "or frozen", "or fresh", "or dried",
    "each", "such as", "optional",
    "(", ")", "(,", ",(",
}

PREP_PREFIX_RE = re.compile(
    r"^(?:chopped|sliced|diced|minced|grated|shredded|crushed|peeled|seeded|"
    r"cubed|halved|quartered|finely chopped|coarsely chopped|thinly sliced)\s+",
    re.IGNORECASE,
)

# Trailing count/portion units that should be stripped from the ingredient name.
# Excludes "leaf", "leaves", "stick", "sticks", "fillet", "fillets" — those
# bear real form information ("bay leaf", "cinnamon stick", "salmon fillet").
TRAILING_UNIT_RE = re.compile(
    r"^(.+?)\s+(?:clove|cloves|slice|slices|wedge|wedges|sprig|sprigs|"
    r"piece|pieces|bulb|bulbs|stalk|stalks|ear|ears|head|heads|sheet|sheets|"
    r"bunch|bunches|strip|strips)$",
    re.IGNORECASE,
)
# Tails to keep — anything where stripping the trailing unit would yield a
# token that is itself a modifier or non-ingredient.
TRAILING_UNIT_KEEP_TAILS = {"ground", "whole", "fresh", "dried", "frozen"}

ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
LEADING_MODIFIER_RE = re.compile(r"^(?:fresh|whole)\s+(?=\S+)", re.IGNORECASE)
TRAILING_CONNECTIVE_RE = re.compile(r"\s+(?:and|or)\s*$", re.IGNORECASE)
PARENS_RE = re.compile(r"\s*\([^)]*\)")
_FRACTIONS = "¼½¾⅓⅔⅛⅜⅝⅞⅙⅚"
QTY_RE = re.compile(
    r"^\s*(?:[" + _FRACTIONS + r"]|\d+(?:[\.,/]\d+)?(?:[\.,]\d+)?)\s*"
    r"(?:to\s+(?:[" + _FRACTIONS + r"]|\d+(?:[\.,/]\d+)?)\s+)?"
    r"(?:cup|cups|tbsp|tablespoon|tablespoons|tsp|teaspoon|teaspoons|"
    r"oz|ounce|ounces|gram|grams|g|kg|pound|pounds|lb|lbs|"
    r"ml|liter|liters|l|dash|pinch|can|cans|clove|cloves|"
    r"large|medium|small|whole|piece|pieces|slice|slices|stick|sticks)?\s+",
    re.IGNORECASE,
)
WS_RE = re.compile(r"\s+")
X_SUFFIX_RE = re.compile(r"\s+X\s*$")
TRIM_PUNCT = ".,;:!?-_"


def _strip_plural(s: str) -> str:
    # only the trivial cases; leave irregulars (mice / mouse) alone
    if s.endswith("ies") and len(s) > 4:
        return s[:-3] + "y"
    if s.endswith("ses") and len(s) > 4 and s[-4] not in "aeiou":
        return s[:-2]
    if s.endswith("oes") and len(s) > 4:
        return s[:-2]
    # leave Latin singulars (-us, -is) and double-s (-ss) alone:
    # asparagus, couscous, hummus, octopus, convolvulus, basis, oasis, analysis.
    if s.endswith(("us", "is", "ss")):
        return s
    if s.endswith("s") and len(s) > 3:
        return s[:-1]
    return s


def normalize(raw: str) -> str | None:
    if not raw:
        return None
    s = X_SUFFIX_RE.sub("", raw).strip()
    s = PARENS_RE.sub(" ", s)
    s = QTY_RE.sub("", s)
    s = ARTICLE_RE.sub("", s)

    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if parts:
            first = parts[0].lower()
            if first in USDA_CATEGORY_WORDS and len(parts) > 1:
                # join "vegetables, mixed" -> "vegetables mixed", "chicken, ground" -> "chicken ground"
                s = f"{parts[0]} {parts[1]}"
            else:
                s = parts[0]

    s = s.strip(TRIM_PUNCT).strip()
    s = WS_RE.sub(" ", s).lower()
    s = ARTICLE_RE.sub("", s)
    s = PREP_PREFIX_RE.sub("", s)
    m = TRAILING_UNIT_RE.match(s)
    if m:
        head = m.group(1).strip()
        last_word = head.split()[-1] if head else ""
        if last_word not in TRAILING_UNIT_KEEP_TAILS:
            s = head
    # strip qualifier prefixes (after trailing-unit removal so "fresh thyme sprig" → "thyme")
    while True:
        new = LEADING_MODIFIER_RE.sub("", s)
        if new == s:
            break
        s = new
    s = TRAILING_CONNECTIVE_RE.sub("", s)
    s = _strip_plural(s)
    s = s.strip(TRIM_PUNCT).strip("()").strip()

    if s in NOISE_TOKENS:
        return None
    if not s or any(c.isdigit() for c in s) and len(s) <= 4:
        return None
    return s


def _driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )


FETCH_CYPHER = """
MATCH (rec:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE rec.source IN $sources
WITH i.name AS name, count(DISTINCT rec) AS uses
RETURN name, uses
"""

REWRITE_CYPHER = """
UNWIND $rows AS row
CALL (row) {
    MATCH (old:Ingredient {name: row.raw_name})
    WHERE old.name <> row.clean_name
    WITH old, row LIMIT 1
    MERGE (clean:Ingredient {name: row.clean_name})
        ON CREATE SET clean.canonical_id = randomUUID()
    WITH old, clean
    MATCH (rec:Recipe)-[h:HAS_INGREDIENT]->(old)
    WHERE rec.source IN $sources
    WITH old, clean, rec, h, properties(h) AS props
    CREATE (rec)-[h2:HAS_INGREDIENT]->(clean)
    SET h2 = props
    DELETE h
} IN TRANSACTIONS OF 500 ROWS
"""

DROP_NOISE_RELS_CYPHER = """
MATCH (rec:Recipe)-[h:HAS_INGREDIENT]->(i:Ingredient {name: $name})
WHERE rec.source IN $sources
WITH h LIMIT 50000
DELETE h
RETURN count(*) AS deleted
"""

DROP_ORPHANS_CYPHER = """
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
    p.add_argument("--sources", nargs="+", default=list(DEFAULT_SOURCES))
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    drv = _driver()
    try:
        with drv.session() as s:
            rows = [(r["name"], r["uses"]) for r in s.run(FETCH_CYPHER, sources=list(args.sources))]
        print(f"distinct ingredient names attached to {args.sources}: {len(rows)}")

        plan: list[dict] = []
        noise_names: list[str] = []
        merges_by_clean: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for raw, uses in rows:
            clean = normalize(raw)
            if clean is None:
                noise_names.append(raw)
                continue
            if clean != raw:
                plan.append({"raw_name": raw, "clean_name": clean, "uses": uses})
            merges_by_clean[clean].append((raw, uses))

        print(f"plan rows (raw→clean rename): {len(plan)}")
        print(f"noise names (rels will be dropped): {len(noise_names)}")
        merge_groups = sum(1 for vs in merges_by_clean.values() if len(vs) > 1)
        print(f"merge groups (>=2 raw names mapping to same clean): {merge_groups}")
        print("\nsample renames:")
        for r in sorted(plan, key=lambda r: -r["uses"])[:15]:
            print(f"  {r['raw_name'][:60]:60s} -> {r['clean_name']:30s} ({r['uses']} uses)")
        print("\nsample noise (will be dropped):")
        for n in noise_names[:10]:
            print(f"  {n!r}")

        if not args.write:
            print("\n[dry-run] re-run with --write to apply.")
            return 0

        # apply renames in chunks
        with drv.session() as s:
            BATCH = 200
            written = 0
            for i in range(0, len(plan), BATCH):
                chunk = plan[i : i + BATCH]
                s.run(REWRITE_CYPHER, rows=chunk, sources=list(args.sources)).consume()
                written += len(chunk)
                if written % 1000 == 0 or written == len(plan):
                    print(f"  renamed {written}/{len(plan)}")
            # drop rels pointing at noise nodes
            n_dropped = 0
            for noise in noise_names:
                while True:
                    rec = s.run(DROP_NOISE_RELS_CYPHER, name=noise, sources=list(args.sources)).single()
                    deleted = rec["deleted"] if rec else 0
                    n_dropped += deleted
                    if not deleted:
                        break
            print(f"dropped {n_dropped} HAS_INGREDIENT rels pointing at noise nodes")
            # cleanup orphans
            total_orphan = 0
            while True:
                rec = s.run(DROP_ORPHANS_CYPHER).single()
                deleted = rec["deleted"] if rec else 0
                total_orphan += deleted
                if not deleted:
                    break
            print(f"deleted {total_orphan} orphan Ingredient nodes")
    finally:
        drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
