#!/usr/bin/env python3
"""Sync allergen evidence and consumer suitability from Neo4j to recipes_v2.

The script adds mappings and writes only when ``--apply`` is supplied.
Existing recipe fields are preserved through Elasticsearch bulk updates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.consumer_suitability import (
    SUITABILITY_CLASSIFICATION_VERSION,
)
from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.es_recipe_evidence import (
    RECIPE_EVIDENCE_MAPPING_PROPERTIES,
    normalize_allergen_evidence,
    normalize_consumer_suitability,
    suitable_groups,
)

load_runtime_env()

QUERY = """
MATCH (r:Recipe)
WHERE r.recipe_id IS NOT NULL
CALL {
  WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
                 -[:HAS_DECLARATION]->(d:AllergenDeclaration)
                 -[:CONCERNS]->(a:Allergen)
  RETURN collect(DISTINCT CASE WHEN a IS NULL THEN NULL ELSE {
    allergen: a.name,
    ingredient: i.name,
    ingredient_id: i.canonical_id,
    declaration_id: d.declaration_id,
    presence: d.presence,
    evidence_status: d.evidence_status,
    sources: d.sources,
    foodon_ids: d.foodon_ids,
    keyword_matches: d.keyword_matches,
    classification_version: d.classification_version
  } END) AS allergen_evidence
}
CALL {
  WITH r
  OPTIONAL MATCH (r)-[s:SUITABILITY_FOR]->(g:ConsumerGroup)
  WHERE g.name IN ["vegan", "vegetarian"]
    AND s.classification_version = $suitability_version
  RETURN collect(DISTINCT CASE WHEN g IS NULL THEN NULL ELSE {
    group: g.name,
    status: s.status,
    blocking_ingredients: s.blocking_ingredients,
    reason_codes: s.reason_codes,
    sources: s.sources,
    classification_version: s.classification_version
  } END) AS consumer_suitability
}
RETURN toString(r.recipe_id) AS id,
       allergen_evidence,
       consumer_suitability
"""


def _neo4j_driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("NEO4J_PASSWORD is required")
    return GraphDatabase.driver(
        uri,
        auth=(username, password),
        notifications_min_severity="OFF",
    )


def _document_fields(record: Any) -> dict[str, Any]:
    allergen_evidence = normalize_allergen_evidence(
        record["allergen_evidence"]
    )
    assessments = normalize_consumer_suitability(
        record["consumer_suitability"],
        classification_version=SUITABILITY_CLASSIFICATION_VERSION,
    )
    return {
        "allergens": sorted(
            {item["allergen"] for item in allergen_evidence}
        ),
        "allergen_evidence": allergen_evidence,
        "suitable_for": suitable_groups(assessments),
        "consumer_suitability": assessments,
    }


def _put_mapping(es_url: str, index: str) -> None:
    response = requests.put(
        f"{es_url.rstrip('/')}/{index}/_mapping",
        json={"properties": RECIPE_EVIDENCE_MAPPING_PROPERTIES},
        timeout=60,
    )
    response.raise_for_status()


def _send_batch(
    es_url: str,
    index: str,
    batch: list[tuple[str, dict[str, Any]]],
) -> tuple[int, int]:
    lines: list[str] = []
    for recipe_id, fields in batch:
        lines.append(
            json.dumps(
                {"update": {"_index": index, "_id": recipe_id}},
                ensure_ascii=False,
            )
        )
        lines.append(json.dumps({"doc": fields}, ensure_ascii=False))
    response = requests.post(
        f"{es_url.rstrip('/')}/_bulk",
        headers={"Content-Type": "application/x-ndjson"},
        data=("\n".join(lines) + "\n").encode("utf-8"),
        timeout=180,
    )
    response.raise_for_status()
    ok = failed = 0
    for item in response.json().get("items", []):
        operation = item.get("update") or {}
        if 200 <= int(operation.get("status", 500)) < 300:
            ok += 1
        else:
            failed += 1
            if failed <= 10:
                print(
                    f"failed id={operation.get('_id')} "
                    f"error={operation.get('error')}"
                )
    return ok, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync recipe allergen evidence and consumer suitability."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--es-url",
        default=os.getenv("ELASTIC_URL", "http://localhost:9200"),
    )
    parser.add_argument(
        "--index",
        default=os.getenv("ELASTIC_INDEX", "recipes_v2"),
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    graph = _neo4j_driver()
    try:
        with graph.session(fetch_size=args.batch_size) as session:
            total_record = session.run(
                "MATCH (r:Recipe) RETURN count(r) AS count"
            ).single()
            total = int(total_record["count"]) if total_record else 0
            if args.limit is not None:
                total = min(total, max(0, args.limit))
            print(
                f"recipes={total} index={args.index} "
                f"mode={'apply' if args.apply else 'dry-run'}"
            )
            if not args.apply:
                return

            _put_mapping(args.es_url, args.index)
            result = session.run(
                QUERY,
                suitability_version=SUITABILITY_CLASSIFICATION_VERSION,
            )
            batch: list[tuple[str, dict[str, Any]]] = []
            ok = failed = processed = 0
            for record in result:
                if args.limit is not None and processed >= args.limit:
                    break
                recipe_id = str(record["id"])
                batch.append((recipe_id, _document_fields(record)))
                processed += 1
                if len(batch) >= args.batch_size:
                    batch_ok, batch_failed = _send_batch(
                        args.es_url, args.index, batch
                    )
                    ok += batch_ok
                    failed += batch_failed
                    batch.clear()
                    if processed % 50000 < args.batch_size:
                        print(
                            f"processed={processed}/{total} "
                            f"updated={ok} failed={failed}"
                        )
            if batch:
                batch_ok, batch_failed = _send_batch(
                    args.es_url, args.index, batch
                )
                ok += batch_ok
                failed += batch_failed
            requests.post(
                f"{args.es_url.rstrip('/')}/{args.index}/_refresh",
                timeout=60,
            ).raise_for_status()
            print(f"done processed={processed} updated={ok} failed={failed}")
            if failed:
                raise RuntimeError(f"{failed} Elasticsearch updates failed")
    finally:
        graph.close()


if __name__ == "__main__":
    main()
