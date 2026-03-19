"""Centralized environment bootstrap for repo/local .env files."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

_LOCK = Lock()
_LOADED = False


def load_runtime_env() -> None:
    """Load .env files once and normalize shared env conventions."""
    global _LOADED
    if _LOADED:
        return

    with _LOCK:
        if _LOADED:
            return

        if load_dotenv is not None:
            api_dir = Path(__file__).resolve().parents[1] / "api"
            repo_root = Path(__file__).resolve().parents[3]
            load_dotenv(api_dir / ".env")
            load_dotenv(repo_root / ".env")
            load_dotenv()

        # Compatibility: support single NEO4J_AUTH="user/pass" env format.
        neo4j_auth = os.getenv("NEO4J_AUTH")
        if neo4j_auth and "/" in neo4j_auth:
            username, password = neo4j_auth.split("/", 1)
            os.environ.setdefault("NEO4J_USERNAME", username)
            os.environ.setdefault("NEO4J_PASSWORD", password)

        _LOADED = True
