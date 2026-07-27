#!/usr/bin/env python3
"""Synchronize EU, Irish, and Hungarian Nutri-Scores from Postgres to Elasticsearch.

Postgres is the source of truth. Existing Elasticsearch recipe documents are
updated in place; recipes absent from Elasticsearch are skipped and never
created by this command.

Usage:
    PYTHONPATH=src python scripts/elasticsearch/sync_nutri_scores_from_postgres.py
    PYTHONPATH=src python scripts/elasticsearch/sync_nutri_scores_from_postgres.py --write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import fetch_all_recipe_scores

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
DEFAULT_INDEX = os.getenv("ELASTIC_INDEX", "recipes_v2")

REGION_FIELDS = {
    "eu": ("nutri_score_eu", "nutri_color_eu"),
    "ie": ("nutri_score_ie", "nutri_color_ie"),
    "hu": ("nutri_score_hu", "nutri_color_hu"),
}


def score_document(profile: dict[str, Any]) -> dict[str, str] | None:
    """Return the six Elasticsearch fields, or None when a region is incomplete."""
    document: dict[str, str] = {}
    for region, (score_field, color_field) in REGION_FIELDS.items():
        regional = profile.get(region)
        if not isinstance(regional, dict):
            return None
        score = str(regional.get("nutri_score") or "").strip()
        color = str(regional.get("nutri_color") or "").strip()
        if not score or not color:
            return None
        document[score_field] = score
        document[color_field] = color
    return document


def iter_updates(
    scores: dict[str, dict[str, Any]],
    index: str,
) -> tuple[Iterator[str], int, int]:
    complete: list[tuple[str, dict[str, str]]] = []
    incomplete = 0
    for recipe_id, profile in scores.items():
        document = score_document(profile)
        if document is None:
            incomplete += 1
            continue
        complete.append((str(recipe_id), document))

    def lines() -> Iterator[str]:
        for recipe_id, document in complete:
            yield json.dumps(
                {"update": {"_index": index, "_id": recipe_id}},
                ensure_ascii=False,
            )
            yield json.dumps({"doc": document}, ensure_ascii=False)

    return lines(), len(complete), incomplete


def batched(values: Iterable[str], size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def obsolete_us_cleanup_request() -> dict[str, Any]:
    """Remove legacy US keys, including keys whose stored value is null."""
    return {
        "script": {
            "lang": "painless",
            "source": (
                "if (ctx._source.containsKey('nutri_score_us') || "
                "ctx._source.containsKey('nutri_color_us')) { "
                "ctx._source.remove('nutri_score_us'); "
                "ctx._source.remove('nutri_color_us'); "
                "} else { ctx.op = 'noop'; }"
            ),
        },
        # `exists` cannot find null-valued _source keys. Scan all documents and
        # let the script mark documents without either legacy key as no-ops.
        "query": {"match_all": {}},
    }


def synchronize(
    scores: dict[str, dict[str, Any]],
    *,
    es_url: str,
    index: str,
    batch_size: int,
    write: bool,
) -> dict[str, int]:
    lines, complete, incomplete = iter_updates(scores, index)
    summary = {
        "postgres_complete": complete,
        "postgres_incomplete": incomplete,
        "updated": 0,
        "absent_from_elasticsearch": 0,
        "obsolete_us_removed": 0,
        "failed": 0,
    }
    if not write:
        return summary

    bulk_url = f"{es_url.rstrip('/')}/_bulk"
    headers = {"Content-Type": "application/x-ndjson"}
    for line_batch in batched(lines, max(2, batch_size * 2)):
        response = requests.post(
            bulk_url,
            headers=headers,
            data=("\n".join(line_batch) + "\n").encode("utf-8"),
            timeout=120,
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            result = item.get("update") or {}
            status = int(result.get("status", 500))
            if 200 <= status < 300:
                summary["updated"] += 1
            elif status == 404:
                summary["absent_from_elasticsearch"] += 1
            else:
                summary["failed"] += 1
                print(
                    f"failed id={result.get('_id')} status={status}: "
                    f"{result.get('error')}",
                    file=sys.stderr,
                )

    cleanup = requests.post(
        f"{es_url.rstrip('/')}/{index}/_update_by_query",
        params={"conflicts": "proceed", "refresh": "true"},
        json=obsolete_us_cleanup_request(),
        timeout=120,
    )
    cleanup.raise_for_status()
    cleanup_result = cleanup.json()
    summary["obsolete_us_removed"] = int(cleanup_result.get("updated", 0))
    summary["failed"] += len(cleanup_result.get("failures", []))

    requests.post(
        f"{es_url.rstrip('/')}/{index}/_refresh",
        timeout=30,
    ).raise_for_status()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply updates (default: dry-run)")
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    scores = fetch_all_recipe_scores()
    summary = synchronize(
        scores,
        es_url=args.es_url,
        index=args.index,
        batch_size=args.batch_size,
        write=args.write,
    )
    print(json.dumps({"write": args.write, **summary}, indent=2))
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
