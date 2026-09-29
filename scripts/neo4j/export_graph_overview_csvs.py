#!/usr/bin/env python3
"""Export graph-level overview CSVs (top ingredients, allergens, tags) for infographics.

Writes four CSVs into ``artifacts/viz/``:

- ``top_ingredients_per_source.csv``  : top N ingredients by recipe count, overall and per source.
- ``top_allergens_per_source.csv``    : recipe-count per allergen, overall and per source.
- ``top_dish_type_tags_per_source.csv``: tag counts where ``Tag.category = 'dish-type'``.
- ``top_dietary_tags_per_source.csv`` : tag counts where ``Tag.category = 'dietary'``.

Each CSV is long-form: ``(scope, name, recipe_count)`` where ``scope`` is
``ALL`` or a current ``Recipe.source`` value discovered from Neo4j.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

import os  # noqa: E402

from neo4j import GraphDatabase  # noqa: E402

OUT_DIR = REPO_ROOT / "artifacts" / "viz"
DEFAULT_TOP_N = 30


def _driver():
    uri = os.environ["NEO4J_URI"]
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ["NEO4J_PASSWORD"]
    return GraphDatabase.driver(uri, auth=(user, pwd))


def _run_scope(
    session, cypher_template: str, top_n: int, sources: list[str]
) -> pd.DataFrame:
    rows: list[dict] = []
    overall = session.run(cypher_template.format(filter=""), top_n=top_n).data()
    for r in overall:
        rows.append({"scope": "ALL", "name": r["name"], "recipe_count": r["n"]})
    for src in sources:
        res = session.run(
            cypher_template.format(filter="WHERE rec.source = $source"),
            top_n=top_n,
            source=src,
        ).data()
        for r in res:
            rows.append({"scope": src, "name": r["name"], "recipe_count": r["n"]})
    return pd.DataFrame.from_records(rows)


INGREDIENTS_Q = """
MATCH (rec:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
{filter}
WITH i.name AS name, count(DISTINCT rec) AS n
RETURN name, n
ORDER BY n DESC
LIMIT $top_n
"""

ALLERGENS_Q = """
MATCH (rec:Recipe)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
{filter}
WITH a.name AS name, count(DISTINCT rec) AS n
RETURN name, n
ORDER BY n DESC
LIMIT $top_n
"""

TAGS_Q = """
MATCH (rec:Recipe)-[:HAS_TAG]->(t:Tag)
WHERE t.category = '{category}'{extra_filter}
WITH t.name AS name, count(DISTINCT rec) AS n
RETURN name, n
ORDER BY n DESC
LIMIT $top_n
"""


def _run_tags_scope(
    session, category: str, top_n: int, sources: list[str]
) -> pd.DataFrame:
    rows: list[dict] = []
    res = session.run(
        TAGS_Q.format(category=category, extra_filter=""), top_n=top_n
    ).data()
    for r in res:
        rows.append({"scope": "ALL", "name": r["name"], "recipe_count": r["n"]})
    for src in sources:
        res = session.run(
            TAGS_Q.format(
                category=category, extra_filter=" AND rec.source = $source"
            ),
            top_n=top_n,
            source=src,
        ).data()
        for r in res:
            rows.append({"scope": src, "name": r["name"], "recipe_count": r["n"]})
    return pd.DataFrame.from_records(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = p.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    drv = _driver()
    try:
        with drv.session() as s:
            sources = [
                row["source"]
                for row in s.run(
                    "MATCH (r:Recipe) WHERE r.source IS NOT NULL "
                    "RETURN DISTINCT r.source AS source ORDER BY source"
                )
            ]
            ing = _run_scope(s, INGREDIENTS_Q, args.top_n, sources)
            alg = _run_scope(s, ALLERGENS_Q, args.top_n, sources)
            dish = _run_tags_scope(s, "dish-type", args.top_n, sources)
            diet = _run_tags_scope(s, "dietary", args.top_n, sources)
    finally:
        drv.close()

    targets = {
        "top_ingredients_per_source.csv": ing,
        "top_allergens_per_source.csv": alg,
        "top_dish_type_tags_per_source.csv": dish,
        "top_dietary_tags_per_source.csv": diet,
    }
    for name, df in targets.items():
        path = args.out_dir / name
        try:
            df.to_csv(path, index=False)
            print(f"wrote {path.relative_to(REPO_ROOT)}  rows={len(df)}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {path}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
