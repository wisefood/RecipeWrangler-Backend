"""Sync ES `image_url` and `source_id` fields from Neo4j.

For every Recipe in Neo4j that also exists in ES (by recipe_id == _id), compare
`image_url` and `source_id` values and issue partial _bulk updates only for the
fields that actually differ.

Scope: ALL recipes (including recipe1m) — unlike the dish_type sync which was
scoped to non-recipe1m sources. Rationale: source_id is the authoritative
collection URN and should be present on every doc; image_url should match the
Neo4j value for every doc.

Usage
-----
  # Dry-run (default)
  python scripts/elasticsearch/sync_image_url_source_id_from_neo4j.py

  # Apply
  python scripts/elasticsearch/sync_image_url_source_id_from_neo4j.py --apply

  # Only one field (useful for narrowing blast radius)
  python scripts/elasticsearch/sync_image_url_source_id_from_neo4j.py --apply --fields source_id
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import run_query

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NEO4J_PAGE_SIZE = 20000
ES_MGET_BATCH = 500
BULK_BATCH_SIZE = 1000
ALLOWED_FIELDS = {"image_url", "source_id"}


def fetch_neo4j_page(skip: int, limit: int) -> list[dict]:
    """Return {recipe_id, image_url, source_id} for a Neo4j page."""
    query = """
    MATCH (r:Recipe)
    WHERE r.image_url IS NOT NULL OR r.source_id IS NOT NULL
    RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
           r.image_url AS image_url,
           r.source_id AS source_id
    ORDER BY recipe_id
    SKIP $skip LIMIT $limit
    """
    return run_query(query, {"skip": skip, "limit": limit})


def fetch_es_current(es_url: str, index: str, recipe_ids: list[str],
                     fields: list[str]) -> dict[str, dict]:
    """Return {_id: {field: value}} from ES for the given IDs."""
    current: dict[str, dict] = {}
    for i in range(0, len(recipe_ids), ES_MGET_BATCH):
        batch = recipe_ids[i:i + ES_MGET_BATCH]
        resp = requests.post(
            f"{es_url}/{index}/_mget",
            json={"docs": [{"_id": rid, "_source": fields} for rid in batch]},
            timeout=60,
        )
        resp.raise_for_status()
        for doc in resp.json().get("docs", []):
            if doc.get("found"):
                current[doc["_id"]] = doc.get("_source", {}) or {}
    return current


def bulk_update(es_url: str, index: str, updates: list[tuple[str, dict]]) -> dict:
    """Apply updates via ES _bulk. updates: list of (_id, {field: new_value, ...})."""
    stats = {"updated": 0, "errors": 0, "not_found": 0}
    for i in range(0, len(updates), BULK_BATCH_SIZE):
        batch = updates[i:i + BULK_BATCH_SIZE]
        lines: list[str] = []
        for rid, doc in batch:
            lines.append(json.dumps({"update": {"_id": rid, "_index": index}}))
            lines.append(json.dumps({"doc": doc}))
        body = "\n".join(lines) + "\n"
        resp = requests.post(
            f"{es_url}/_bulk",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            op = item.get("update", {})
            status = op.get("status")
            if status in (200, 201):
                stats["updated"] += 1
            elif status == 404:
                stats["not_found"] += 1
            else:
                stats["errors"] += 1
                logger.warning("ES bulk error id=%s status=%s error=%s",
                               op.get("_id"), status, op.get("error"))
        logger.info("Bulk progress: updated=%d not_found=%d errors=%d",
                    stats["updated"], stats["not_found"], stats["errors"])
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually send updates to ES. Default is dry-run.")
    parser.add_argument("--es-url",
                        default=os.getenv("ELASTIC_URL", "http://localhost:9200"))
    parser.add_argument("--index",
                        default=os.getenv("ELASTIC_INDEX", "recipes"))
    parser.add_argument("--fields",
                        default="image_url,source_id",
                        help="Comma-separated list of fields to sync "
                             "(subset of image_url,source_id)")
    args = parser.parse_args()

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    invalid = set(fields) - ALLOWED_FIELDS
    if invalid or not fields:
        logger.error("Invalid fields: %s. Allowed: %s", invalid or "(empty)", ALLOWED_FIELDS)
        sys.exit(2)

    logger.info("ES: %s  index=%s  fields=%s", args.es_url, args.index, fields)

    # Pre-flight: index reachable?
    try:
        resp = requests.get(f"{args.es_url}/{args.index}", timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("ES not reachable: %s", exc)
        sys.exit(1)

    # Totals for sanity
    total_resp = requests.get(f"{args.es_url}/{args.index}/_count", timeout=10)
    total_resp.raise_for_status()
    es_total = total_resp.json().get("count", 0)
    neo_total_rows = run_query("MATCH (r:Recipe) RETURN count(r) AS n", {})
    neo_total = neo_total_rows[0]["n"] if neo_total_rows else 0
    logger.info("Neo4j has %d recipes, ES has %d docs", neo_total, es_total)

    # Stream through Neo4j in pages, checking ES for each page
    to_update: list[tuple[str, dict]] = []
    stats = {
        "neo4j_scanned": 0,
        "es_missing": 0,
        "unchanged": 0,
        "per_field_changes": {f: 0 for f in fields},
    }
    sample_diffs: list[tuple[str, dict, dict]] = []

    t0 = time.monotonic()
    skip = 0
    while True:
        page = fetch_neo4j_page(skip, NEO4J_PAGE_SIZE)
        if not page:
            break
        skip += NEO4J_PAGE_SIZE
        stats["neo4j_scanned"] += len(page)

        ids = [str(r["recipe_id"]) for r in page]
        es_docs = fetch_es_current(args.es_url, args.index, ids, fields)

        for row in page:
            rid = str(row["recipe_id"])
            if rid not in es_docs:
                stats["es_missing"] += 1
                continue
            es_doc = es_docs[rid]
            diff: dict = {}
            for f in fields:
                want = row.get(f)
                have = es_doc.get(f)
                if want is None:
                    continue  # don't clobber ES with null from Neo4j
                if have != want:
                    diff[f] = want
                    stats["per_field_changes"][f] += 1
            if diff:
                to_update.append((rid, diff))
                if len(sample_diffs) < 10:
                    sample_diffs.append((rid, es_doc, diff))
            else:
                stats["unchanged"] += 1

        if stats["neo4j_scanned"] % 100000 == 0 or not page:
            logger.info("Progress: scanned=%d queued=%d unchanged=%d missing=%d (%.1fs)",
                        stats["neo4j_scanned"], len(to_update),
                        stats["unchanged"], stats["es_missing"],
                        time.monotonic() - t0)

    # Summary
    logger.info("=== Plan ===")
    logger.info("Neo4j scanned          : %d", stats["neo4j_scanned"])
    logger.info("Missing in ES (skipped): %d", stats["es_missing"])
    logger.info("Already correct        : %d", stats["unchanged"])
    logger.info("Will update (docs)     : %d", len(to_update))
    for f in fields:
        logger.info("  field %-12s changes: %d", f, stats["per_field_changes"][f])

    for rid, before, diff in sample_diffs:
        logger.info("  sample: %s  before=%s  →  %s", rid, before, diff)

    if not args.apply:
        logger.info("Dry-run only. Re-run with --apply to write.")
        return

    if not to_update:
        logger.info("No changes to apply.")
        return

    logger.info("=== Applying %d bulk updates ===", len(to_update))
    t1 = time.monotonic()
    result = bulk_update(args.es_url, args.index, to_update)
    logger.info("Done in %.1fs: %s", time.monotonic() - t1, result)


if __name__ == "__main__":
    main()
