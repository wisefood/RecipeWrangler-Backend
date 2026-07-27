#!/usr/bin/env python3
"""Add and populate normalized recipe titles for exact Elasticsearch lookup.

Dry-run is the default. Pass ``--write`` to add the mapping and update existing
profiled recipe documents in place.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.tools.es_recipe_search import normalize_recipe_title


DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
DEFAULT_INDEX = os.getenv("ELASTIC_INDEX", "recipes_v2")


def _missing_query() -> dict[str, Any]:
    return {
        "bool": {
            "filter": [
                {"exists": {"field": "nutri_score_eu"}},
                {"exists": {"field": "title"}},
            ],
            "must_not": [{"exists": {"field": "title_normalized"}}],
        }
    }


def _count_missing(es_url: str, index: str) -> int:
    response = requests.post(
        f"{es_url}/{index}/_count",
        json={"query": _missing_query()},
        timeout=30,
    )
    response.raise_for_status()
    return int(response.json().get("count", 0))


def _bulk_update(es_url: str, index: str, hits: list[dict[str, Any]]) -> tuple[int, int]:
    lines: list[str] = []
    for hit in hits:
        title = str((hit.get("_source") or {}).get("title") or "").strip()
        normalized = normalize_recipe_title(title)
        lines.append(
            json.dumps(
                {"update": {"_index": index, "_id": hit["_id"]}},
                ensure_ascii=False,
            )
        )
        lines.append(
            json.dumps(
                {"doc": {"title_normalized": normalized}},
                ensure_ascii=False,
            )
        )
    if not lines:
        return 0, 0

    response = requests.post(
        f"{es_url}/_bulk",
        data=("\n".join(lines) + "\n").encode(),
        headers={"Content-Type": "application/x-ndjson"},
        timeout=120,
    )
    response.raise_for_status()
    updated = failed = 0
    for item in response.json().get("items", []):
        operation = item.get("update") or {}
        if 200 <= int(operation.get("status", 500)) < 300:
            updated += 1
        else:
            failed += 1
    return updated, failed


def backfill(es_url: str, index: str, batch_size: int) -> tuple[int, int]:
    response = requests.post(
        f"{es_url}/{index}/_search",
        params={"scroll": "2m"},
        json={
            "size": batch_size,
            "_source": ["title"],
            "sort": ["_doc"],
            "query": _missing_query(),
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    scroll_id = payload.get("_scroll_id")
    updated = failed = 0

    try:
        while True:
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                break
            batch_updated, batch_failed = _bulk_update(es_url, index, hits)
            updated += batch_updated
            failed += batch_failed
            response = requests.post(
                f"{es_url}/_search/scroll",
                json={"scroll": "2m", "scroll_id": scroll_id},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            scroll_id = payload.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            requests.delete(
                f"{es_url}/_search/scroll",
                json={"scroll_id": [scroll_id]},
                timeout=30,
            )
    return updated, failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    es_url = args.es_url.rstrip("/")
    missing = _count_missing(es_url, args.index)
    print(f"missing_title_normalized={missing}")
    if not args.write:
        print("Dry run only; pass --write to update Elasticsearch.")
        return

    mapping_response = requests.put(
        f"{es_url}/{args.index}/_mapping",
        json={"properties": {"title_normalized": {"type": "keyword"}}},
        timeout=30,
    )
    mapping_response.raise_for_status()
    updated, failed = backfill(es_url, args.index, max(1, args.batch_size))
    requests.post(f"{es_url}/{args.index}/_refresh", timeout=60).raise_for_status()
    print(f"updated={updated} failed={failed}")
    print(f"remaining_missing={_count_missing(es_url, args.index)}")


if __name__ == "__main__":
    main()
