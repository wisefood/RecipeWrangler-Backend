"""Redis-backed recipe cache.

Only active when RECIPE_CACHE_ENABLED=true. All public functions are safe to call
unconditionally — they become no-ops when caching is disabled or Redis is unreachable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

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


def _key(recipe_id: str, variant: str | None = None) -> str:
    key = f"recipe:{recipe_id}"
    return f"{key}:{variant}" if variant else key


def cache_get(recipe_id: str, variant: str | None = None) -> dict[str, Any] | None:
    if not _is_enabled():
        return None
    try:
        raw = _get_client().get(_key(recipe_id, variant))
        return json.loads(raw) if raw else None
    except Exception:
        logger.warning("Redis cache_get failed for %s", recipe_id, exc_info=True)
        return None


def cache_set(
    recipe_id: str,
    data: dict[str, Any],
    ttl_seconds: int = 3600,
    variant: str | None = None,
) -> None:
    if not _is_enabled():
        return
    try:
        _get_client().setex(_key(recipe_id, variant), ttl_seconds, json.dumps(data))
    except Exception:
        logger.warning("Redis cache_set failed for %s", recipe_id, exc_info=True)


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
