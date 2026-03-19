"""FastAPI dependency providers."""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from recipe_wrangler.tools.text2cypher import RecipeSearchAppV2
from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.utils.env_loader import load_runtime_env

from .config import get_settings

load_runtime_env()


def get_recipe_search_app() -> RecipeSearchAppV2:
    """FastAPI dependency entry-point for the recipe search tool."""

    try:
        return _get_recipe_search_app_cached()
    except RuntimeError as exc:
        raise map_dependency_error("Neo4j/Groq search app", exc) from exc


@lru_cache(maxsize=1)
def _get_recipe_search_app_cached() -> RecipeSearchAppV2:
    """Instantiate and cache the recipe search tool."""

    settings = get_settings()
    _assert_neo4j_reachable(str(settings.neo4j_uri), settings.neo4j_connect_timeout)
    _assert_groq_key()
    return RecipeSearchAppV2(
        neo4j_uri=str(settings.neo4j_uri),
        model=settings.search_main_model,
        temperature=settings.search_temperature,
    )


def _assert_neo4j_reachable(neo4j_uri: str, timeout: float) -> None:
    """Fail fast if the Neo4j bolt endpoint cannot be reached."""

    if not neo4j_uri.startswith("bolt://"):
        raise RuntimeError("NEO4J_URI must start with bolt://")

    host_port = neo4j_uri[len("bolt://") :]
    host, _, port_str = host_port.partition(":")
    port = int(port_str) if port_str else 7687

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:  # bubble up as runtime error so FastAPI can convert
        raise RuntimeError(f"Unable to reach Neo4j at {host}:{port}: {exc}") from exc


def _assert_groq_key() -> None:
    """Fail fast if the GROQ_API_KEY env var is missing."""

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is not set; add it to your environment or .env.")
