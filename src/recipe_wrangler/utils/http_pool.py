# Purpose: Shared pooled HTTP session for Elasticsearch and other HTTP data backends.

import os
import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

_session: Optional[requests.Session] = None
_lock = threading.Lock()


def get_http_session() -> requests.Session:
    """Return the process-wide pooled requests session.

    Reuses keep-alive TCP connections to HTTP data backends (Elasticsearch)
    instead of opening a fresh connection per request. Pool sizing is
    env-configurable:
      HTTP_POOL_CONNECTIONS (default 10) — distinct hosts pooled
      HTTP_POOL_MAXSIZE     (default 20) — connections kept per host
    """
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                adapter = HTTPAdapter(
                    pool_connections=int(os.getenv("HTTP_POOL_CONNECTIONS", "10")),
                    pool_maxsize=int(os.getenv("HTTP_POOL_MAXSIZE", "20")),
                )
                session = requests.Session()
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                _session = session
    return _session


def post_query_with_retry(
    url: str,
    json_body: dict,
    timeout: float,
    attempts: int = 2,
) -> requests.Response:
    """POST a read-only query, retrying once on a read timeout.

    ONLY for idempotent queries (ES _search / autocomplete): reissuing them is
    always safe, and single-node Elasticsearch GC pauses cause one-off
    multi-second stalls that a single retry rides out. Never use for writes.
    """
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return get_http_session().post(url, json=json_body, timeout=timeout)
        except requests.exceptions.ReadTimeout as exc:
            last_exc = exc
    raise last_exc
