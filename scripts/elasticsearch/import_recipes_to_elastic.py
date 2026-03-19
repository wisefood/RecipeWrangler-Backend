#!/usr/bin/env python3
"""Bulk import recipe documents into Elasticsearch without client-version headers.

This avoids elasticsearch-py v9 compatibility headers against ES 8.x.

first in dev tools:
PUT /recipes
{
  "mappings": {
    "properties": {
      "id": { "type": "keyword" },
      "title": { 
        "type": "search_as_you_type" 
      },
      "ingredients": { "type": "text" },
      "tags": { "type": "keyword" }
    }
  }
}

then test :

GET /recipes/_search
{
  "query": {
    "multi_match": {
      "query": "mac and ch",
      "type": "bool_prefix",
      "fields": [
        "title",
        "title._2gram",
        "title._3gram"
      ]
    }
  }
}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import requests


DEFAULT_INPUT = Path("data/processed/elasticsearch/recipes_for_elasticsearch.json")
DEFAULT_ES_URL = "http://localhost:9200"
DEFAULT_INDEX = "recipes"


def _iter_bulk_lines(recipes: Iterable[dict], index: str) -> Iterable[str]:
    for recipe in recipes:
        rid = str(recipe.get("id") or "").strip()
        if not rid:
            continue

        action = {"index": {"_index": index, "_id": rid}}
        source = {
            "id": rid,
            "title": recipe.get("title", ""),
            "ingredients": recipe.get("ingredients", []),
            "tags": recipe.get("tags", []),
        }
        yield json.dumps(action, ensure_ascii=False)
        yield json.dumps(source, ensure_ascii=False)


def _chunks(items: list[str], chunk_size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import recipes JSON into Elasticsearch.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--es-url", type=str, default=DEFAULT_ES_URL)
    parser.add_argument("--index", type=str, default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    recipes = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(recipes, list):
        raise ValueError(f"Input JSON must be a list, got: {type(recipes).__name__}")

    lines = list(_iter_bulk_lines(recipes, args.index))
    if not lines:
        print("No valid recipes found to import.")
        return

    headers = {"Content-Type": "application/x-ndjson"}
    bulk_url = f"{args.es_url.rstrip('/')}/_bulk"

    total_indexed = 0
    total_failed = 0

    print("Starting import...")
    line_batch_size = max(2, args.batch_size * 2)  # 2 lines per document
    for batch in _chunks(lines, line_batch_size):
        body = "\n".join(batch) + "\n"
        resp = requests.post(bulk_url, headers=headers, data=body.encode("utf-8"), timeout=120)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        for item in items:
            op = item.get("index") or item.get("create") or {}
            status = int(op.get("status", 500))
            if 200 <= status < 300:
                total_indexed += 1
            else:
                total_failed += 1

    print(f"Done. indexed={total_indexed} failed={total_failed}")


if __name__ == "__main__":
    main()
