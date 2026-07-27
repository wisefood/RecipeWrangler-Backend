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
        raise map_dependency_error("Groq constraint extractor", exc) from exc


@lru_cache(maxsize=1)
def _get_recipe_constraint_extractor_cached() -> RecipeConstraintExtractor:
    settings = get_settings()
    _assert_groq_key()
    return RecipeConstraintExtractor(
        model=settings.search_main_model,
        temperature=settings.search_temperature,
    )


def _assert_groq_key() -> None:
    """Fail fast if the GROQ_API_KEY env var is missing."""

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is not set; add it to your environment or .env.")
