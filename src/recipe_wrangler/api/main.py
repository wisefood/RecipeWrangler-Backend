"""FastAPI application exposing RecipeWrangler services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load env before importing heavy dependencies that expect keys.
API_DIR = Path(__file__).resolve().parent
load_dotenv(API_DIR / ".env")
load_dotenv()  # fallback to repo-level .env

from recipe_wrangler.tools.text2cypher import RecipeSearchApp
from recipe_wrangler.tools.parse_recipe_tool import parse_recipe_tool_open
from recipe_wrangler.tools.fetch_recipe_info import (
    fetch_recipe_info,
    fetch_recipe_info_by_id,
)
from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain

from .config import get_settings
from .dependencies import get_recipe_search_app
from .schemas import (
    ParseRecipeRequest,
    ParseRecipeResponse,
    RecipeProfileRequest,
    RecipeProfileResponse,
    RecipeSearchRequest,
    RecipeSearchResponse,
    RecipeDetailResponse,
)


def _extract_title(candidate: dict[str, object]) -> str | None:
    """Best-effort extraction of a recipe title from a LangGraph result row."""

    title = candidate.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    for key, value in candidate.items():
        if isinstance(value, str) and "title" in key.lower():
            return value
        if isinstance(value, dict):
            nested_title = value.get("title")
            if isinstance(nested_title, str) and nested_title.strip():
                return nested_title.strip()

    return None


def _attach_recipe_metadata(results: list[object]) -> list[object]:
    """Augment each result row with full recipe metadata when possible."""

    cache: dict[str, dict] = {}
    enriched: list[object] = []

    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue

        recipe_id = entry.get("id") if isinstance(entry.get("id"), str) else None
        title = _extract_title(entry)
        cache_key = recipe_id or title

        if not cache_key:
            enriched.append(entry)
            continue

        if cache_key not in cache:
            metadata: dict[str, Any] = {}
            try:
                if recipe_id:
                    metadata = fetch_recipe_info_by_id(recipe_id) or {}
                if not metadata and title:
                    metadata = fetch_recipe_info(title) or {}
            except Exception:
                metadata = {}

            cache[cache_key] = metadata

        metadata = cache.get(cache_key) or {}
        if metadata:
            combined = dict(entry)
            combined["recipe_info"] = metadata
            enriched.append(combined)
        else:
            enriched.append(entry)

    return enriched


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    app = FastAPI(title="RecipeWrangler API", version="0.2.0")
    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["ops"])
    def healthcheck() -> dict[str, str]:
        """Simple readiness probe."""

        return {"status": "ok"}

    @app.post(
        "/api/v1/recipes/parse",
        response_model=ParseRecipeResponse,
        tags=["recipes"],
        summary="Parse unstructured recipe text into structured fields",
    )
    def recipe_parse(payload: ParseRecipeRequest) -> ParseRecipeResponse:
        """Call the parse recipe tool and normalize its output."""

        try:
            result = parse_recipe_tool_open.invoke({"raw_recipe": payload.raw_recipe})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Recipe parsing failed: {exc}",
            ) from exc

        if not isinstance(result, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Recipe parser returned unexpected payload",
            )

        response_payload = {
            "title": result.get("title", ""),
            "ingredient_names": result.get("ingredient_names", []),
            "measurements": result.get("measurements", []),
            "directions": result.get("directions", []),
            "total_time": result.get("total_time"),
        }

        return ParseRecipeResponse(**response_payload)

    @app.get(
        "/api/v1/recipes/{recipe_id}",
        response_model=RecipeDetailResponse,
        tags=["recipes"],
        summary="Retrieve a recipe with full metadata by id",
    )
    def get_recipe(recipe_id: str) -> RecipeDetailResponse:
        try:
            recipe = fetch_recipe_info_by_id(recipe_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch recipe: {exc}",
            ) from exc

        if not recipe:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipe not found",
            )

        return RecipeDetailResponse(**recipe)

    @app.post(
        "/api/v1/recipes/search",
        response_model=RecipeSearchResponse,
        tags=["recipes"],
        summary="Search recipes via the knowledge graph",
    )
    def recipe_search(
        payload: RecipeSearchRequest,
        recipe_search_app: RecipeSearchApp = Depends(get_recipe_search_app),
    ) -> RecipeSearchResponse:
        """Invoke the recipe search LangGraph pipeline and return its output."""

        try:
            result = recipe_search_app.invoke(payload.question)
        except Exception as exc:  # noqa: BLE001 - bubble up as HTTP error
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Recipe search failed: {exc}",
            ) from exc
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Recipe search returned unexpected payload",
            )

        raw_results = result.get("results", [])
        if isinstance(raw_results, list):
            hydrated_results = _attach_recipe_metadata(raw_results)
        else:
            hydrated_results = raw_results

        response_payload = {
            "results": hydrated_results,
            "steps": result.get("steps", []),
            "cypher_statement": result.get("cypher_statement", ""),
        }
        return RecipeSearchResponse(**response_payload)

    @app.post(
        "/api/v1/recipes/profile",
        response_model=RecipeProfileResponse,
        tags=["recipes"],
        summary="Run parsing + profiling pipeline on raw recipe text",
    )
    def recipe_profile(payload: RecipeProfileRequest) -> RecipeProfileResponse:
        """Execute the Recipe_Profiling_Chain on raw recipe text."""

        try:
            profile_result = Recipe_Profiling_Chain.invoke(
                {"recipe_text": payload.raw_recipe, "debug": False}
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Recipe profiling failed: {exc}",
            ) from exc

        if not isinstance(profile_result, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Recipe profiling returned unexpected payload",
            )

        ingredients_list = profile_result.get("ingredients") or []
        ingredient_weights: dict[str, Any] = {}
        for item in ingredients_list:
            if not isinstance(item, dict):
                continue
            name = item.get("ingredient") or item.get("name")
            if not name:
                continue
            ingredient_weights[str(name)] = item.get("weight_g")

        directions = profile_result.get("directions")
        if isinstance(directions, list):
            normalized_directions = [str(step) for step in directions]
        elif isinstance(directions, str):
            normalized_directions = [step.strip() for step in directions.split("\n") if step.strip()]
        else:
            normalized_directions = []

        response_payload = {
            "title": profile_result.get("title"),
            "serves": profile_result.get("serves"),
            "duration_min": profile_result.get("total_time"),
            "ingredients_grams": ingredient_weights,
            "directions": normalized_directions,
            "profiling_totals": profile_result.get("profiling_totals") or {},
            "tags": [str(tag) for tag in profile_result.get("tags", []) if str(tag).strip()],
        }

        return RecipeProfileResponse(**response_payload)

    return app


app = create_app()
"""Public ASGI entry-point for `uvicorn api.main:app`."""


if __name__ == "__main__":  # convenience for local runs
    import uvicorn

    port = int(os.getenv("PORT", "8001"))  # default to 8001 to avoid clashing with Chroma
    uvicorn.run("recipe_wrangler.api.main:app", host="0.0.0.0", port=port, reload=True)
