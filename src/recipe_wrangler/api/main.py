"""FastAPI application exposing RecipeWrangler services."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from recipe_wrangler.api.routers.generic import install_error_handler
from dotenv import load_dotenv
import recipe_wrangler.api.logsys as logsys
import uvicorn

# Load env before importing heavy dependencies that expect keys.
API_DIR = Path(__file__).resolve().parent
load_dotenv(API_DIR / ".env")
load_dotenv()  # fallback to repo-level .env

from recipe_wrangler.tools.text2cypher import RecipeSearchApp
from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain
from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
from recipe_wrangler.utils.nutrition_postgres import fetch_recipe_nutrition_by_id

from .config import get_settings
from .dependencies import get_recipe_search_app
from recipe_wrangler.schemas import (
    RecipeDetailResponse,
    RecipeProfileRequest,
    RecipeProfileResponse,
    RecipeSearchRequest,
    RecipeSearchResponse,
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


def _nutri_color_from_score(nutri_score: object) -> str | None:
    if isinstance(nutri_score, dict):
        color = nutri_score.get("color")
        return color if isinstance(color, str) and color.strip() else None

    if isinstance(nutri_score, str):
        score = nutri_score.strip()
        mapping = {
            "Nutriscore_A": "dark green",
            "Nutriscore_B": "green",
            "Nutriscore_C": "yellow",
            "Nutriscore_D": "orange",
            "Nutriscore_E": "dark orange",
        }
        return mapping.get(score)

    return None


def _attach_nutri_colors(results: list[object]) -> list[object]:
    """Add nutri_score color from Postgres nutrition data when available."""

    cache: dict[str, str | None] = {}
    enriched: list[object] = []

    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue

        recipe_id = entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        if not recipe_id:
            enriched.append(entry)
            continue

        if recipe_id not in cache:
            color = None
            try:
                nutrition = fetch_recipe_nutrition_by_id(recipe_id)
            except Exception:
                nutrition = None
            if nutrition:
                color = _nutri_color_from_score(nutrition.get("nutri_score"))
            cache[recipe_id] = color

        combined = dict(entry)
        combined["nutri_color"] = cache.get(recipe_id)
        enriched.append(combined)

    return enriched


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _normalize_nutrients(total_nutrients: object) -> list[dict[str, object]]:
    if not isinstance(total_nutrients, dict):
        return []

    nutrients = total_nutrients.get("nutrients", total_nutrients)
    if isinstance(nutrients, dict):
        normalized = []
        for name, info in nutrients.items():
            if not name:
                continue
            if isinstance(info, dict):
                value = info.get("value")
                if value is None:
                    value = info.get("nutrient_value")
                if value is None:
                    value = info.get("amount")
                unit = info.get("unit") or info.get("nutrient_unit")
            else:
                value = info
                unit = None
            normalized.append({"name": str(name), "value": value, "unit": unit})
        return normalized

    if isinstance(nutrients, list):
        normalized = []
        for item in nutrients:
            if not isinstance(item, dict):
                continue
            name = item.get("nutrient_description") or item.get("nutrient_name") or item.get("name")
            if not name:
                continue
            value = item.get("value")
            if value is None:
                value = item.get("nutrient_value")
            if value is None:
                value = item.get("amount")
            unit = item.get("unit") or item.get("nutrient_unit")
            normalized.append({"name": str(name), "value": value, "unit": unit})
        return normalized

    return []


def _extract_nutrient_value(total_nutrients: object, names: list[str]) -> float | None:
    candidates = {name.lower() for name in names}
    for entry in _normalize_nutrients(total_nutrients):
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if name.lower() not in candidates:
            continue
        value = entry.get("value")
        parsed = _coerce_float(value)
        if parsed is not None:
            return parsed
    return None


def _per_serving(value: float | None, serves: object) -> float | None:
    if value is None:
        return None
    servings = _coerce_float(serves)
    if servings is None or servings <= 0:
        return value
    return value / servings


def _coerce_nutri_score(nutri_score: object) -> float | None:
    numeric = _coerce_float(nutri_score)
    if numeric is not None:
        return numeric

    if isinstance(nutri_score, dict):
        numeric = _coerce_float(nutri_score.get("score"))
        if numeric is not None:
            return numeric
        grade = nutri_score.get("nutri_score")
        if isinstance(grade, str):
            nutri_score = grade

    if isinstance(nutri_score, str):
        mapping = {
            "Nutriscore_A": 1.0,
            "Nutriscore_B": 0.75,
            "Nutriscore_C": 0.5,
            "Nutriscore_D": 0.25,
            "Nutriscore_E": 0.0,
            "A": 1.0,
            "B": 0.75,
            "C": 0.5,
            "D": 0.25,
            "E": 0.0,
        }
        return mapping.get(nutri_score.strip())

    return None




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
from .config import get_settings
from .routers import health, recipes

# Get settings
settings = get_settings()
logsys.configure()

# Create FastAPI app
app = FastAPI(title="RecipeWrangler API", version="0.2.0")

    @app.get(
        "/api/recipes/{recipe_id}",
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

        nutrition = None
        try:
            nutrition = fetch_recipe_nutrition_by_id(recipe_id)
        except Exception:
            nutrition = None

        payload = dict(recipe)
        if nutrition:
            total_nutrients = nutrition.get("total_nutrients")
            serves = payload.get("serves")
            payload.update(
                {
                    "total_kcal_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Energy", "Energy (kcal)", "Energy, kcal"],
                    ),
                    "total_protein_g_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Protein"],
                    ),
                    "total_carbs_g_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Carbohydrate", "Carbohydrate, by difference", "Carbohydrate, by diff."],
                    ),
                    "total_fat_g_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Total lipid (fat)", "Fat", "Total fat"],
                    ),
                    "total_fiber_g_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Fiber, total dietary", "Dietary Fiber", "Fiber"],
                    ),
                    "total_sugar_g_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Sugars, total", "Sugars, total including NLEA", "Sugars, total NLEA"],
                    ),
                    "total_sodium_mg_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Sodium, Na", "Sodium"],
                    ),
                    "total_cholesterol_mg_per_serving": _extract_nutrient_value(
                        total_nutrients,
                        ["Cholesterol"],
                    ),
                    "nutri_score": _coerce_nutri_score(nutrition.get("nutri_score")),
                }
            )
            payload["total_kcal_per_serving"] = _per_serving(payload.get("total_kcal_per_serving"), serves)
            payload["total_protein_g_per_serving"] = _per_serving(payload.get("total_protein_g_per_serving"), serves)
            payload["total_carbs_g_per_serving"] = _per_serving(payload.get("total_carbs_g_per_serving"), serves)
            payload["total_fat_g_per_serving"] = _per_serving(payload.get("total_fat_g_per_serving"), serves)
            payload["total_fiber_g_per_serving"] = _per_serving(payload.get("total_fiber_g_per_serving"), serves)
            payload["total_sugar_g_per_serving"] = _per_serving(payload.get("total_sugar_g_per_serving"), serves)
            payload["total_sodium_mg_per_serving"] = _per_serving(payload.get("total_sodium_mg_per_serving"), serves)
            payload["total_cholesterol_mg_per_serving"] = _per_serving(payload.get("total_cholesterol_mg_per_serving"), serves)

        return RecipeDetailResponse(**payload)

    @app.post(
        "/api/recipes/search",
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
            raw_results = _attach_nutri_colors(raw_results)

        response_payload = {
            "results": raw_results,
            "steps": result.get("steps", []),
            "cypher_statement": result.get("cypher_statement", ""),
        }
        return RecipeSearchResponse(**response_payload)

    @app.post(
        "/api/recipes/profile",
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
install_error_handler(app)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health.router)
app.include_router(recipes.router)

if __name__ == "__main__":
    uvicorn.run("recipe_wrangler.api.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8001")), reload=True)
