"""Redis-backed recipe cache.

Only active when RECIPE_CACHE_ENABLED=true. All public functions are safe to call
unconditionally — they become no-ops when caching is disabled or Redis is unreachable.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import redis

logger = logging.getLogger(__name__)

_pool: redis.ConnectionPool | None = None
_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _pool, _client  # noqa: PLW0603

    if _client is not None:
        return _client

    from recipe_wrangler.api.config import get_settings

    settings = get_settings()
    _pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        db=settings.redis_recipe_db,
        decode_responses=True,
        max_connections=10,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    _client = redis.Redis(connection_pool=_pool)
    return _client


def _is_enabled() -> bool:
    from recipe_wrangler.api.config import get_settings
    return get_settings().recipe_cache_enabled


def _default_ttl() -> int:
    from recipe_wrangler.api.config import get_settings
    return get_settings().redis_recipe_ttl


def _key(recipe_id: str, variant: str | None = None) -> str:
    key = f"recipe:{recipe_id}"
    return f"{key}:{variant}" if variant else key


def raw_cache_get(key: str) -> str | None:
    """Fetch an arbitrary string value (non-recipe keyspace, e.g. nlq:*)."""
    if not _is_enabled():
        return None
    try:
        return _get_client().get(key)
    except Exception:
        logger.warning("Redis raw_cache_get failed for %s", key, exc_info=True)
        return None


def raw_cache_setex(key: str, ttl_seconds: int, value: str) -> None:
    """Store an arbitrary string value with a TTL (non-recipe keyspace)."""
    if not _is_enabled():
        return
    try:
        _get_client().setex(key, ttl_seconds, value)
    except Exception:
        logger.warning("Redis raw_cache_setex failed for %s", key, exc_info=True)


def cache_get(recipe_id: str, variant: str | None = None) -> dict[str, Any] | None:
    if not _is_enabled():
        return None
    try:
        raw = _get_client().get(_key(recipe_id, variant))
        return json.loads(raw) if raw else None
    except Exception:
        logger.warning("Redis cache_get failed for %s", recipe_id, exc_info=True)
        return None


def cache_mget(
    recipe_ids: Iterable[str],
    variant: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Bulk fetch via a single MGET. Returns {recipe_id: data} for hits only."""
    if not _is_enabled():
        return {}
    ids = [rid for rid in recipe_ids if rid]
    if not ids:
        return {}
    try:
        keys = [_key(rid, variant) for rid in ids]
        raws = _get_client().mget(keys)
    except Exception:
        logger.warning("Redis cache_mget failed", exc_info=True)
        return {}

    hits: dict[str, dict[str, Any]] = {}
    for rid, raw in zip(ids, raws):
        if not raw:
            continue
        try:
            hits[rid] = json.loads(raw)
        except Exception:
            continue
    return hits


def cache_set(
    recipe_id: str,
    data: dict[str, Any],
    ttl_seconds: int | None = None,
    variant: str | None = None,
) -> None:
    if not _is_enabled():
        return
    ttl = ttl_seconds if ttl_seconds is not None else _default_ttl()
    try:
        _get_client().setex(_key(recipe_id, variant), ttl, json.dumps(data))
    except Exception:
        logger.warning("Redis cache_set failed for %s", recipe_id, exc_info=True)


def cache_mset(
    entries: dict[str, dict[str, Any]],
    ttl_seconds: int | None = None,
    variant: str | None = None,
) -> None:
    """Bulk write via a pipeline. Each entry gets the same TTL."""
    if not _is_enabled() or not entries:
        return
    ttl = ttl_seconds if ttl_seconds is not None else _default_ttl()
    try:
        client = _get_client()
        pipe = client.pipeline(transaction=False)
        for rid, data in entries.items():
            if not rid:
                continue
            pipe.setex(_key(rid, variant), ttl, json.dumps(data))
        pipe.execute()
    except Exception:
        logger.warning("Redis cache_mset failed (%d entries)", len(entries), exc_info=True)


def cache_delete_many(recipe_ids: Iterable[str]) -> None:
    """Bulk delete base entries and every variant key for the given IDs.

    One keyspace SCAN total — per-ID cache_delete scans the whole keyspace
    for each recipe, which is prohibitive for bulk status flips.
    """
    if not _is_enabled():
        return
    ids = {str(rid) for rid in recipe_ids if rid}
    if not ids:
        return
    try:
        client = _get_client()
        # Key shape is recipe:{id}[:{variant}] — match on the id segment.
        doomed = [
            key for key in client.scan_iter(match="recipe:*", count=1000)
            if key.split(":", 2)[1] in ids
        ]
        for start in range(0, len(doomed), 1000):
            client.delete(*doomed[start:start + 1000])
    except Exception:
        logger.warning("Redis cache_delete_many failed (%d ids)", len(ids), exc_info=True)


def cache_delete(recipe_id: str, variant: str | None = None) -> None:
    if not _is_enabled():
        return
    try:
        client = _get_client()
        if variant:
            client.delete(_key(recipe_id, variant))
            return

        keys = [_key(recipe_id)]
        keys.extend(client.scan_iter(match=f"{_key(recipe_id)}:*", count=100))
        client.delete(*keys)
    except Exception:
        logger.warning("Redis cache_delete failed for %s", recipe_id, exc_info=True)
