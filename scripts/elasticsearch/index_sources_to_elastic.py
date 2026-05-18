#!/usr/bin/env python3
"""Index recipes from specific Neo4j sources into Elasticsearch.

Queries Neo4j for recipes matching the given source labels and bulk-indexes
them directly — useful when new sources are added after the initial full import.

Usage:
    python scripts/elasticsearch/index_sources_to_elastic.py --sources FoodHero HealthyFoods
    python scripts/elasticsearch/index_sources_to_elastic.py --sources Irish_SafeFood
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv
from neo4j import GraphDatabase


REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
DEFAULT_INDEX = os.getenv("ELASTIC_INDEX", "recipes")
DEFAULT_NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
DEFAULT_NEO4J_USERNAME = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
DEFAULT_NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

QUERY = """
MATCH (r:Recipe)
WHERE r.source IN $sources
  AND coalesce(toString(r.recipe_id), toString(r.id)) IS NOT NULL
WITH r, coalesce(toString(r.recipe_id), toString(r.id)) AS rid
OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
WITH r, rid, collect(DISTINCT coalesce(toString(i.name), toString(i.title))) AS ingredients
OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
RETURN
  rid AS id,
  coalesce(toString(r.title), "") AS title,
  coalesce(toString(r.image_url), "") AS image_url,
  coalesce(toString(r.source), "") AS source,
  coalesce(toString(r.source_id), "") AS source_id,
  ingredients,
  collect(DISTINCT coalesce(toString(t.name), toString(t.title))) AS tags
ORDER BY id
"""


def _clean_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        item = _clean_str(v)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _iter_bulk_lines(recipes: Iterable[dict], index: str) -> Iterable[str]:
    for recipe in recipes:
        rid = recipe.get("id", "").strip()
        if not rid:
            continue
        yield json.dumps({"index": {"_index": index, "_id": rid}}, ensure_ascii=False)
        yield json.dumps(
            {
                "id": rid,
                "title": recipe["title"],
                "image_url": recipe["image_url"],
                "source": recipe["source"],
                "source_id": recipe["source_id"],
                "ingredients": recipe["ingredients"],
                "tags": recipe["tags"],
            },
            ensure_ascii=False,
        )


def fetch_from_neo4j(sources: list[str], uri: str, username: str, password: str) -> list[dict]:
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        with driver.session() as session:
            rows = list(session.run(QUERY, sources=sources))
        return [
            {
                "id": _clean_str(row["id"]),
                "title": _clean_str(row["title"]),
                "image_url": _clean_str(row["image_url"]),
                "source": _clean_str(row["source"]),
                "source_id": _clean_str(row["source_id"]),
                "ingredients": _clean_list(row["ingredients"]),
                "tags": _clean_list(row["tags"]),
            }
            for row in rows
        ]
    finally:
        driver.close()


def bulk_index(recipes: list[dict], es_url: str, index: str, batch_size: int) -> tuple[int, int]:
    lines = list(_iter_bulk_lines(recipes, index))
    if not lines:
        return 0, 0

    bulk_url = f"{es_url.rstrip('/')}/_bulk"
    headers = {"Content-Type": "application/x-ndjson"}
    line_batch = max(2, batch_size * 2)

    total_ok = total_fail = 0
    for start in range(0, len(lines), line_batch):
        body = "\n".join(lines[start : start + line_batch]) + "\n"
        resp = requests.post(bulk_url, headers=headers, data=body.encode("utf-8"), timeout=120)
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            op = item.get("index") or item.get("create") or {}
            if 200 <= int(op.get("status", 500)) < 300:
                total_ok += 1
            else:
                total_fail += 1

    return total_ok, total_fail


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Neo4j recipes by source into Elasticsearch.")
    parser.add_argument("--sources", nargs="+", required=True, help="Neo4j r.source values to index")
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--neo4j-uri", default=DEFAULT_NEO4J_URI)
    parser.add_argument("--neo4j-username", default=DEFAULT_NEO4J_USERNAME)
    parser.add_argument("--neo4j-password", default=DEFAULT_NEO4J_PASSWORD)
    args = parser.parse_args()

    if not args.neo4j_password:
        raise ValueError("Missing Neo4j password. Set NEO4J_PASSWORD or pass --neo4j-password.")

    print(f"Fetching from Neo4j: sources={args.sources}")
    recipes = fetch_from_neo4j(
        sources=args.sources,
        uri=args.neo4j_uri,
        username=args.neo4j_username,
        password=args.neo4j_password,
    )
    print(f"Found {len(recipes)} recipes in Neo4j")

    if not recipes:
        print("Nothing to index.")
        return

    ok, fail = bulk_index(recipes, es_url=args.es_url, index=args.index, batch_size=args.batch_size)
    print(f"Done. indexed={ok} failed={fail}")


if __name__ == "__main__":
    main()
