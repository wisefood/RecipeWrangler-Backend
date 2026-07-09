"""Recipe status (soft-delete) shared predicates and Elasticsearch sync.

A recipe carries `status` = 'active' | 'disabled' (Neo4j property, ES keyword
field). Missing/legacy status means active — every read site filters with the
shared snippets below so the convention stays uniform and greppable.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from recipe_wrangler.utils.http_pool import get_http_session

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# Neo4j predicate — append with AND to any recipe-serving WHERE clause.
NEO4J_NOT_DISABLED = "coalesce(r.status, 'active') <> 'disabled'"

_ES_BULK_BATCH = 1000
# In-ES retries per doc when a concurrent writer bumps the version between the
# update's read and write. Status writes are last-write-wins, so retrying is
# always safe.
_ES_RETRY_ON_CONFLICT = 3
# Per-index cap on individually logged item failures — a corpus-scale job can
# fail tens of thousands of docs the same way, and one warning per doc evicts
# everything else from the pod's log buffer.
_ES_LOGGED_ERRORS_CAP = 10


def es_not_disabled_clause() -> dict[str, Any]:
    """ES must_not clause hiding disabled recipes (absent status == active)."""
    return {"term": {"status": STATUS_DISABLED}}


class StatusJobGuard:
    """Single-flight guard for corpus-scale by-query status jobs.

    Two overlapping jobs race doc-by-doc in ES (endless 409 log floods) and
    double the load on Neo4j/ES for zero benefit — a duplicate POST (retry,
    double-click) must be rejected, not queued. In-process only: correct for
    a single replica, and per replica if the deployment ever scales out.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Any] | None = None

    def try_claim(self, status: str, requested: int) -> dict[str, Any] | None:
        """Claim the job slot. Returns None on success; on contention returns
        the running job's info ({status, requested, running_for_s}) unclaimed."""
        with self._lock:
            if self._active is not None:
                return {
                    "status": self._active["status"],
                    "requested": self._active["requested"],
                    "running_for_s": time.monotonic() - self._active["started"],
                }
            self._active = {
                "status": status,
                "requested": requested,
                "started": time.monotonic(),
            }
            return None

    def release(self) -> None:
        with self._lock:
            self._active = None


status_job_guard = StatusJobGuard()


def _ensure_status_mapping(es_url: str, index: str) -> None:
    """Add the `status` keyword field to an index that predates it (additive,
    no-op if present; ignored if the index already mapped it dynamically)."""
    try:
        get_http_session().put(
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
    Returns per-index stats: {index: {updated, not_found, conflicts, errors}}.
    """
    all_stats: dict[str, dict[str, int]] = {}
    for index in indices:
        _ensure_status_mapping(es_url, index)
        stats = {"updated": 0, "not_found": 0, "conflicts": 0, "errors": 0}
        for start in range(0, len(recipe_ids), _ES_BULK_BATCH):
            batch = recipe_ids[start:start + _ES_BULK_BATCH]
            lines: list[str] = []
            for rid in batch:
                lines.append(json.dumps({
                    "update": {
                        "_id": rid,
                        "_index": index,
                        "retry_on_conflict": _ES_RETRY_ON_CONFLICT,
                    }
                }))
                lines.append(json.dumps({"doc": {"status": status}}))
            try:
                resp = get_http_session().post(
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
                elif code == 409:
                    # A concurrent writer won even after in-ES retries. For a
                    # status write both racers set the same terminal value, so
                    # the doc converged — count it, don't page anyone.
                    stats["conflicts"] += 1
                else:
                    stats["errors"] += 1
                    if stats["errors"] <= _ES_LOGGED_ERRORS_CAP:
                        logger.warning(
                            "ES status update error id=%s status=%s error=%s",
                            op.get("_id"), code, op.get("error"),
                        )
        all_stats[index] = stats
        if stats["errors"] > _ES_LOGGED_ERRORS_CAP:
            logger.warning(
                "ES status sync index=%s: %d item errors total (first %d logged)",
                index, stats["errors"], _ES_LOGGED_ERRORS_CAP,
            )
        logger.info("ES status sync index=%s status=%s %s", index, status, stats)
    return all_stats
