"""Sync ElasticSearch dish_type from Neo4j dish-type tags.

Reads every Recipe in Neo4j that has a :HAS_TAG → :Tag{category:'dish-type'}
(skipping EXCLUDED_SOURCES), picks one canonical slot via DISH_TYPE_PRIORITY,
and writes it to ES via the _bulk API.

Usage
-----
  # Dry-run (default): print planned changes only.
  python scripts/elasticsearch/sync_dish_types_from_neo4j.py

  # Actually update ES.
  python scripts/elasticsearch/sync_dish_types_from_neo4j.py --apply

  # Different ES endpoint / index
  python scripts/elasticsearch/sync_dish_types_from_neo4j.py --es-url http://localhost:9200 --index recipes
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


# Sources whose recipes should NOT have their ES dish_type overwritten.
EXCLUDED_SOURCES = {"recipe1m"}

# When Neo4j has multiple dish-type tags for a recipe, pick the first present
# here. ES dish_type is single-valued in this deployment, so we must collapse.
DISH_TYPE_PRIORITY = [
    "breakfast",
    "desserts",
    "main-dish",
    "side-dish",
    "snacks",
    "beverages",
]

BULK_BATCH_SIZE = 500


def fetch_neo4j_tagged_recipes() -> dict[str, str]:
    """Return {recipe_id: chosen_dish_type} for all eligible recipes.

    Eligible = has at least one dish-type tag AND source not in EXCLUDED_SOURCES.
    """
    query = """
    MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag {category:'dish-type'})
    WHERE NOT toLower(coalesce(r.source, '')) IN $excluded
    WITH r, collect(DISTINCT toLower(t.name)) AS slots
    RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id, slots
    """
    rows = run_query(query, {"excluded": [s.lower() for s in EXCLUDED_SOURCES]})

    chosen: dict[str, str] = {}
    for row in rows:
        rid = (row.get("recipe_id") or "").strip()
        slots = row.get("slots") or []
        if not rid or not slots:
            continue
        picked = next((s for s in DISH_TYPE_PRIORITY if s in slots), slots[0])
        chosen[rid] = picked
    return chosen


def fetch_es_current(es_url: str, index: str, recipe_ids: list[str]) -> dict[str, str]:
    """Return {recipe_id: current dish_type} from ES for the given IDs.

    Uses _mget; only docs that exist and have dish_type are returned.
    """
    current: dict[str, str] = {}
    for i in range(0, len(recipe_ids), BULK_BATCH_SIZE):
        batch = recipe_ids[i:i + BULK_BATCH_SIZE]
        resp = requests.post(
            f"{es_url}/{index}/_mget",
            json={
                "docs": [
                    {"_id": rid, "_source": ["dish_type"]} for rid in batch
                ]
            },
            timeout=30,
        )
        resp.raise_for_status()
        for doc in resp.json().get("docs", []):
            if doc.get("found") and doc.get("_source", {}).get("dish_type") is not None:
                current[doc["_id"]] = doc["_source"]["dish_type"]
    return current


def bulk_update(es_url: str, index: str, updates: list[tuple[str, str]]) -> dict:
    """Apply updates via ES _bulk. updates: list of (recipe_id, new_dish_type)."""
    stats = {"updated": 0, "errors": 0, "not_found": 0}
    for i in range(0, len(updates), BULK_BATCH_SIZE):
        batch = updates[i:i + BULK_BATCH_SIZE]
        lines: list[str] = []
        for rid, new_val in batch:
            lines.append(json.dumps({"update": {"_id": rid, "_index": index}}))
            lines.append(json.dumps({"doc": {"dish_type": new_val}}))
        body = "\n".join(lines) + "\n"
        resp = requests.post(
            f"{es_url}/_bulk",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=120,
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
                        default=os.getenv("ELASTIC_URL", "http://localhost:9200"),
                        help="ES base URL (default: env ELASTIC_URL or http://localhost:9200)")
    parser.add_argument("--index",
                        default=os.getenv("ELASTIC_INDEX", "recipes"),
                        help="ES index name (default: env ELASTIC_INDEX or 'recipes')")
    args = parser.parse_args()

    logger.info("ES: %s  index=%s", args.es_url, args.index)

    # Sanity: ES reachable?
    try:
        resp = requests.get(f"{args.es_url}/{args.index}", timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("ES not reachable: %s", exc)
        sys.exit(1)

    t0 = time.monotonic()
    logger.info("Fetching tagged recipes from Neo4j…")
    desired = fetch_neo4j_tagged_recipes()
    logger.info("Neo4j returned %d eligible recipes in %.1fs", len(desired), time.monotonic() - t0)

    if not desired:
        logger.info("Nothing to do.")
        return

    t0 = time.monotonic()
    logger.info("Fetching current ES dish_type for comparison…")
    current = fetch_es_current(args.es_url, args.index, list(desired.keys()))
    logger.info("ES returned %d existing docs in %.1fs", len(current), time.monotonic() - t0)

    # Compute diffs
    to_update: list[tuple[str, str]] = []
    missing_in_es: list[str] = []
    unchanged = 0
    for rid, want in desired.items():
        have = current.get(rid)
        if have is None:
            missing_in_es.append(rid)
            continue
        if have == want:
            unchanged += 1
        else:
            to_update.append((rid, want))

    # Summaries
    logger.info("=== Plan ===")
    logger.info("Eligible in Neo4j      : %d", len(desired))
    logger.info("Present in ES          : %d", len(current))
    logger.info("Missing in ES (skipped): %d", len(missing_in_es))
    logger.info("Already correct        : %d", unchanged)
    logger.info("Will update            : %d", len(to_update))

    # Distribution of target values among to-update
    dist: dict[str, int] = {}
    for _, v in to_update:
        dist[v] = dist.get(v, 0) + 1
    logger.info("Target distribution in updates: %s",
                {k: dist[k] for k in sorted(dist)})

    # Show a handful of sample diffs
    sample = to_update[:10]
    for rid, new_val in sample:
        logger.info("  sample: %s  %r → %r", rid, current.get(rid), new_val)

    if not args.apply:
        logger.info("Dry-run only. Re-run with --apply to write.")
        return

    if not to_update:
        logger.info("No changes to apply.")
        return

    logger.info("=== Applying %d updates via _bulk ===", len(to_update))
    stats = bulk_update(args.es_url, args.index, to_update)
    logger.info("Done: %s", stats)


if __name__ == "__main__":
    main()
