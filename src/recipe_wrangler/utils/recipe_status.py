"""Recipe status (soft-delete) shared predicates and Elasticsearch sync.

A recipe carries `status` = 'active' | 'disabled' (Neo4j property, ES keyword
field). Missing/legacy status means active — every read site filters with the
shared snippets below so the convention stays uniform and greppable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# Neo4j predicate — append with AND to any recipe-serving WHERE clause.
NEO4J_NOT_DISABLED = "coalesce(r.status, 'active') <> 'disabled'"

_ES_BULK_BATCH = 1000


def es_not_disabled_clause() -> dict[str, Any]:
    """ES must_not clause hiding disabled recipes (absent status == active)."""
    return {"term": {"status": STATUS_DISABLED}}


def _ensure_status_mapping(es_url: str, index: str) -> None:
    """Add the `status` keyword field to an index that predates it (additive,
    no-op if present; ignored if the index already mapped it dynamically)."""
    try:
        requests.put(
            f"{es_url}/{index}/_mapping",
            json={"properties": {"status": {"type": "keyword"}}},
            timeout=10,
        )
    except Exception:
        logger.warning("Could not ensure status mapping on %s", index, exc_info=True)


def sync_recipe_status_to_es(
    recipe_ids: list[str],
    status: str,
    *,
    es_url: str,
    indices: list[str],
    timeout: float = 180,
) -> dict[str, dict[str, int]]:
    """Best-effort bulk `status` update for the given recipe IDs on every index.

    Both the primary search index (recipes_v2) and the legacy autocomplete/
    fallback index must be updated or disabled recipes linger in search.
    Returns per-index stats: {index: {updated, not_found, errors}}.
    """
    all_stats: dict[str, dict[str, int]] = {}
    for index in indices:
        _ensure_status_mapping(es_url, index)
        stats = {"updated": 0, "not_found": 0, "errors": 0}
        for start in range(0, len(recipe_ids), _ES_BULK_BATCH):
            batch = recipe_ids[start:start + _ES_BULK_BATCH]
            lines: list[str] = []
            for rid in batch:
                lines.append(json.dumps({"update": {"_id": rid, "_index": index}}))
                lines.append(json.dumps({"doc": {"status": status}}))
            try:
                resp = requests.post(
                    f"{es_url}/_bulk",
                    data="\n".join(lines) + "\n",
                    headers={"Content-Type": "application/x-ndjson"},
                    timeout=timeout,
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
            except Exception:
                stats["errors"] += len(batch)
                logger.warning(
                    "ES status bulk failed index=%s batch=%d..%d",
                    index, start, start + len(batch), exc_info=True,
                )
                continue
            for item in items:
                op = item.get("update", {})
                code = op.get("status")
                if code in (200, 201):
                    stats["updated"] += 1
                elif code == 404:
                    # Not every recipe is indexed (e.g. unprofiled) — expected.
                    stats["not_found"] += 1
                else:
                    stats["errors"] += 1
                    logger.warning(
                        "ES status update error id=%s status=%s error=%s",
                        op.get("_id"), code, op.get("error"),
                    )
        all_stats[index] = stats
        logger.info("ES status sync index=%s status=%s %s", index, status, stats)
    return all_stats
