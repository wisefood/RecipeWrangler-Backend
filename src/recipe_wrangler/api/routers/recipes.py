"""Recipe-related endpoints router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from recipe_wrangler.api.exceptions import InternalError, NotFoundError

from recipe_wrangler.tools.text2cypher import RecipeSearchApp
from recipe_wrangler.tools.fetch_recipe_info import (
    fetch_recipe_info,
    fetch_recipe_info_by_id,
)
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.utils.nutrition_postgres import fetch_recipe_nutrition_by_id
from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain

from .generic import render
from ..dependencies import get_recipe_search_app
from recipe_wrangler.schemas import (
    RecipeProfileRequest,
    RecipeProfileResponse,
    RecipeSearchRequest,
    RecipeSearchResponse,
    RecipeDetailResponse,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


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


def _attach_recipe_scores(results: list[object]) -> list[object]:
    """Attach nutri_score and sust_score (per serving) from Neo4j when possible."""

    if not results:
        return results

    ids: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        if not candidate:
            candidate = entry.get("id") if isinstance(entry.get("id"), str) else None
        if candidate:
            ids.append(candidate)

    if not ids:
        return results

    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id,
           r.nutriscore AS nutri_score,
           r.totalsustainabilityperserving AS sust_score,
           r.duration AS duration,
           r.serves AS serves,
           r.title AS title
    """
    rows = run_query(query, {"ids": ids})
    score_map = {
        str(record.get("recipe_id")): record
        for record in rows
        if record.get("recipe_id") is not None
    }

    enriched: list[object] = []
    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue
        rid = entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        if not rid:
            rid = entry.get("id") if isinstance(entry.get("id"), str) else None
        combined = dict(entry)
        if rid and rid in score_map:
            record = score_map[rid]
            for key in ("nutri_score", "sust_score", "duration", "serves", "title"):
                if combined.get(key) in (None, "") and record.get(key) is not None:
                    combined[key] = record.get(key)
        enriched.append(combined)

    return enriched


def _attach_image_urls(results: list[object]) -> list[object]:
    """Attach image_url for recipe rows by recipe_id/id when possible."""

    if not results:
        return results

    ids: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        if not candidate:
            candidate = entry.get("id") if isinstance(entry.get("id"), str) else None
        if candidate:
            ids.append(candidate)

    if not ids:
        return results

    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id, r.image_url AS image_url
    """
    image_rows = run_query(query, {"ids": ids})
    image_map = {
        str(record.get("recipe_id")): record.get("image_url")
        for record in image_rows
        if record.get("recipe_id") is not None
    }

    enriched: list[object] = []
    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue
        rid = entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        if not rid:
            rid = entry.get("id") if isinstance(entry.get("id"), str) else None
        combined = dict(entry)
        if rid and "image_url" not in combined:
            combined["image_url"] = image_map.get(rid)
        enriched.append(combined)

    return enriched


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

        recipe_id = (
            entry.get("recipe_id") if isinstance(entry.get("recipe_id"), str) else None
        )
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
            name = (
                item.get("nutrient_description")
                or item.get("nutrient_name")
                or item.get("name")
            )
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


@router.get(
    "/{recipe_id}",
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
                    [
                        "Carbohydrate",
                        "Carbohydrate, by difference",
                        "Carbohydrate, by diff.",
                    ],
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
                    [
                        "Sugars, total",
                        "Sugars, total including NLEA",
                        "Sugars, total NLEA",
                    ],
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
        payload["total_kcal_per_serving"] = _per_serving(
            payload.get("total_kcal_per_serving"), serves
        )
        payload["total_protein_g_per_serving"] = _per_serving(
            payload.get("total_protein_g_per_serving"), serves
        )
        payload["total_carbs_g_per_serving"] = _per_serving(
            payload.get("total_carbs_g_per_serving"), serves
        )
        payload["total_fat_g_per_serving"] = _per_serving(
            payload.get("total_fat_g_per_serving"), serves
        )
        payload["total_fiber_g_per_serving"] = _per_serving(
            payload.get("total_fiber_g_per_serving"), serves
        )
        payload["total_sugar_g_per_serving"] = _per_serving(
            payload.get("total_sugar_g_per_serving"), serves
        )
        payload["total_sodium_mg_per_serving"] = _per_serving(
            payload.get("total_sodium_mg_per_serving"), serves
        )
        payload["total_cholesterol_mg_per_serving"] = _per_serving(
            payload.get("total_cholesterol_mg_per_serving"), serves
        )

    return RecipeDetailResponse(**payload)


@router.post(
    "/search",
    response_model=None,
    tags=["recipes"],
    summary="Search recipes via the knowledge graph",
)
def recipe_search(
    payload: RecipeSearchRequest,
    recipe_search_app: RecipeSearchApp = Depends(get_recipe_search_app),
) -> dict[str, Any]:
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
        raw_results = _attach_recipe_scores(raw_results)
        raw_results = _attach_image_urls(raw_results)
        allowed_keys = {
            "recipe_id",
            "title",
            "duration",
            "serves",
            "nutri_score",
            "sust_score",
            "image_url",
        }
        filtered_results = []
        for entry in raw_results:
            if isinstance(entry, dict):
                filtered_results.append(
                    {key: entry.get(key) for key in allowed_keys if key in entry}
                )
            else:
                filtered_results.append(entry)
        raw_results = filtered_results

    return {"results": raw_results}


@router.post(
    "/profile",
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
        normalized_directions = [
            step.strip() for step in directions.split("\n") if step.strip()
        ]
    else:
        normalized_directions = []

    response_payload = {
        "title": profile_result.get("title"),
        "serves": profile_result.get("serves"),
        "duration_min": profile_result.get("total_time"),
        "ingredients_grams": ingredient_weights,
        "directions": normalized_directions,
        "profiling_totals": profile_result.get("profiling_totals") or {},
        "tags": [
            str(tag) for tag in profile_result.get("tags", []) if str(tag).strip()
        ],
    }

    return RecipeProfileResponse(**response_payload)
