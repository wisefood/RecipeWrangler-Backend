#!/usr/bin/env python3
"""Re-sync the `allergens` field in ES recipes_v2 from Neo4j HAS_ALLERGEN edges.

Queries Neo4j in batches (skip/limit), derives each recipe's allergen set from its
ingredient graph, then bulk-updates the ES document. Safe to re-run.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/elasticsearch/sync_allergens_to_es.py
    PYTHONPATH=src .venv/bin/python scripts/elasticsearch/sync_allergens_to_es.py --sources Irish_SafeFood PLANEAT
    PYTHONPATH=src .venv/bin/python scripts/elasticsearch/sync_allergens_to_es.py --batch-size 2000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

import requests

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.utils.neo4j_utils import driver as neo4j_driver

QUERY = """
MATCH (r:Recipe)
WHERE ($sources IS NULL OR r.source IN $sources)
  AND coalesce(toString(r.recipe_id), toString(r.id)) IS NOT NULL
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
  RETURN collect(DISTINCT al.name) AS allergens
}
RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
       allergens
ORDER BY recipe_id
SKIP $skip LIMIT $limit
"""

COUNT_QUERY = """
MATCH (r:Recipe)
WHERE ($sources IS NULL OR r.source IN $sources)
  AND coalesce(toString(r.recipe_id), toString(r.id)) IS NOT NULL
RETURN count(r) AS n
"""


def _bulk_update(settings, batch: list[tuple[str, list[str]]]) -> int:
    lines = []
    for recipe_id, allergens in batch:
        lines.append(f'{{"update":{{"_index":"recipes_v2","_id":"{recipe_id}"}}}}\n')
        lines.append(f'{{"doc":{{"allergens":{allergens}}}}}\n'.replace("'", '"'))
    body = "".join(lines)
    r = requests.post(
        f"{settings.elastic_url}/_bulk",
        headers={"Content-Type": "application/x-ndjson"},
        data=body.encode(),
        timeout=30,
    )
    r.raise_for_status()
    resp = r.json()
    errors = [item for item in resp.get("items", []) if item.get("update", {}).get("error")]
    if errors:
        print(f"  WARN: {len(errors)} bulk errors, e.g.: {errors[0]}")
    return len(batch) - len(errors)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", nargs="+", default=None, help="Restrict to specific source labels")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    settings = get_settings()
    sources = args.sources
    batch_size = args.batch_size

    with neo4j_driver.session() as s:
        total = s.run(COUNT_QUERY, {"sources": sources}).single()["n"]

    print(f"Syncing allergens for {total} recipes (batch={batch_size}, sources={sources or 'all'})")

    done = updated = 0
    skip = 0
    while skip < total:
        with neo4j_driver.session() as s:
            rows = s.run(QUERY, {"sources": sources, "skip": skip, "limit": batch_size}).data()
        if not rows:
            break

        batch = [(row["recipe_id"], row["allergens"] or []) for row in rows]
        n = _bulk_update(settings, batch)
        done += len(rows)
        updated += n
        skip += batch_size
        print(f"  {done}/{total} processed | {updated} updated", flush=True)

    print(f"Done. {updated}/{total} ES docs updated.")


if __name__ == "__main__":
    main()
