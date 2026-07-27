"""FastAPI dependency providers."""

from __future__ import annotations

import os
from functools import lru_cache

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.tools.recipe_search_constraints import RecipeConstraintExtractor
from recipe_wrangler.utils.env_loader import load_runtime_env

from .config import get_settings

load_runtime_env()


def get_recipe_constraint_extractor() -> RecipeConstraintExtractor:
    """Return the Neo4j-independent extractor used by Elasticsearch search."""

    try:
        return _get_recipe_constraint_extractor_cached()
    except RuntimeError as exc:
        raise map_dependency_error("recipe constraint extractor", exc) from exc


@lru_cache(maxsize=1)
def _get_recipe_constraint_extractor_cached() -> RecipeConstraintExtractor:
    settings = get_settings()
    _assert_search_key(settings.search_llm_source)
    return RecipeConstraintExtractor(
        model=settings.search_main_model,
        temperature=settings.search_temperature,
        source=settings.search_llm_source,
    )


def _assert_search_key(source: str) -> None:
    """Fail fast when the configured search provider has no API key."""
    normalized = str(source or "").strip().lower()
    if normalized == "openrouter":
        if not os.getenv("OPENROUTER_API_KEY"):
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set; add it to your environment or .env."
            )
        return
    if normalized == "groq":
        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError(
                "GROQ_API_KEY is not set; add it to your environment or .env."
            )
        return
    raise RuntimeError("SEARCH_LLM_SOURCE must be 'groq' or 'openrouter'.")
