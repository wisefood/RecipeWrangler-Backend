"""Recipe-related endpoints router."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import requests
from fastapi import APIRouter, Query

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.exceptions import (
    DataError,
    InternalError,
    NotFoundError,
)
from recipe_wrangler.api.config import get_settings

from recipe_wrangler.tools.param_search import search_recipes_by_params
from recipe_wrangler.utils.recipe_cache import cache_delete, cache_get, cache_set
from recipe_wrangler.utils.neo4j_utils import run_query as _run_query
from recipe_wrangler.tools.fetch_recipe_info import (
    fetch_recipe_info,
    fetch_recipe_info_by_id,
)
from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergens_from_names,
    fetch_recipe_image_urls_by_ids,
    fetch_recipe_scores_by_ids,
    find_ingredient_substitutes,
    infer_diet_tags,
    update_recipe_in_neo4j,
    upsert_recipe_to_neo4j,
)
from recipe_wrangler.repositories.postgres_nutrition import (
    get_recipe_nutrition,
    get_recipe_profile_trace,
    save_recipe_profile_trace,
)
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent
from recipe_wrangler.repositories.chroma_matchers import query_usda_nutrition_candidates

_USDA_MATCH_THRESHOLD = 0.4
from recipe_wrangler.tools.recipe_profiling_chain import (
    Recipe_Profiling_Chain,
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)

from ..dependencies import get_recipe_search_app
from recipe_wrangler.schemas import (
    RecipeCardResponse,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeDetailResponse,
    RecipeProfileRequest,
    RecipeSearchFilters,
    RecipeSearchRequest,
    RecipeSubstituteRequest,
    RecipeSubstituteResponse,
    RecipeUpdateRequest,
    RecipeUpdateResponse,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])

_RECIPE_BASE_CACHE_VARIANT = "base"


def _profile_meta() -> str:
    settings = get_settings()
    return settings.profile_pipeline_version


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _as_list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _nutrition_source_from_region(region: str | None) -> str | None:
    if region is None:
        return None
    region_norm = str(region).strip().upper()
    if not region_norm:
        return None
    mapping = {"US": "usda", "IE": "irish", "HU": "hungarian"}
    return mapping.get(region_norm)


def _recipe_response_cache_variant(region: str | None, slim: bool) -> str:
    region_key = str(region or "default").strip().upper() or "DEFAULT"
    region_key = "".join(ch if ch.isalnum() else "_" for ch in region_key)
    return f"detail:region:{region_key}:slim:{int(slim)}"


def _cached_recipe_response(
    recipe_id: str,
    variant: str,
    slim: bool,
) -> RecipeDetailResponse | RecipeCardResponse | None:
    cached = cache_get(recipe_id, variant=variant)
    if not cached:
        return None
    try:
        if slim:
            return RecipeCardResponse(**cached)
        return RecipeDetailResponse(**cached)
    except Exception:
        cache_delete(recipe_id, variant=variant)
        return None


def _cache_recipe_response(
    requested_recipe_id: str,
    resolved_recipe_id: str,
    variant: str,
    response: RecipeDetailResponse | RecipeCardResponse,
) -> None:
    data = response.model_dump(mode="json")
    cache_set(requested_recipe_id, data, variant=variant)
    if resolved_recipe_id != requested_recipe_id:
        cache_set(resolved_recipe_id, data, variant=variant)


def _random_myplate_from_elastic(limit: int = 10) -> list[dict[str, Any]]:
    """Fetch random MyPlate recipes directly from Elasticsearch for fast landing results."""
    settings = get_settings()
    safe_limit = max(1, min(int(limit), 50))
    payload = {
        "size": safe_limit,
        "_source": ["id", "title", "image_url"],
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"source.keyword": "myplate"}},
                            {"exists": {"field": "image_url"}},
                        ]
                    }
                },
                "random_score": {},
            }
        },
    }
    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    # Keep startup UX snappy; fail fast to local fallback if ES is slow/unavailable.
    response = requests.post(url, json=payload, timeout=min(settings.elastic_timeout, 1.5))
    response.raise_for_status()
    body = response.json()
    hits = body.get("hits", {}).get("hits", [])

    results: list[dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source", {}) if isinstance(hit, dict) else {}
        rid = _as_id(source.get("id")) or _as_id(hit.get("_id"))
        title = source.get("title")
        image_url = source.get("image_url")
        if not rid:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(image_url, str) or not image_url.strip():
            # Keep startup cards image-complete.
            continue
        results.append(
            {
                "recipe_id": rid,
                "title": title.strip(),
                "source": "myplate",
                "image_url": image_url.strip(),
            }
        )
        if len(results) >= safe_limit:
            break
    return results


def _normalize_search_results(raw_results: list[object]) -> list[object]:
    """Attach metadata and keep only public keys for search responses."""

    raw_results = _attach_nutri_colors(raw_results)
    raw_results = _attach_recipe_scores(raw_results)
    raw_results = _attach_image_urls(raw_results)
    allowed_keys = {
        "recipe_id",
        "title",
        "source",
        "duration",
        "serves",
        "nutri_score",
        "sust_score",
        "image_url",
    }

    filtered_results: list[object] = []
    for entry in raw_results:
        if isinstance(entry, dict):
            # Some pipelines return `id` instead of `recipe_id`.
            # Keep response shape stable for UI routing/card links.
            rid = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
            if rid and _as_id(entry.get("recipe_id")) is None:
                entry = {**entry, "recipe_id": rid}
            filtered_results.append(
                {key: entry.get(key) for key in allowed_keys if key in entry}
            )
        else:
            filtered_results.append(entry)
    return filtered_results


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

        recipe_id = _as_id(entry.get("id"))
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
        candidate = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        if candidate:
            ids.append(candidate)

    if not ids:
        return results

    score_map = fetch_recipe_scores_by_ids(ids)

    enriched: list[object] = []
    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue
        rid = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        combined = dict(entry)
        if rid and rid in score_map:
            record = score_map[rid]
            for key in ("nutri_score", "sust_score", "duration", "serves", "source", "title"):
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
        candidate = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        if candidate:
            ids.append(candidate)

    if not ids:
        return results

    image_map = fetch_recipe_image_urls_by_ids(ids)

    enriched: list[object] = []
    for entry in results:
        if not isinstance(entry, dict):
            enriched.append(entry)
            continue
        rid = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
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

        recipe_id = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        if not recipe_id:
            enriched.append(entry)
            continue

        if recipe_id not in cache:
            color = None
            try:
                nutrition = get_recipe_nutrition(recipe_id)
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
            normalized.append(
                {
                    "name": str(name),
                    "value": value,
                    "unit": unit,
                    "nutrient_name": str(name),
                    "amount_per_serving": value,
                    "unit_name": unit,
                }
            )
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
            normalized.append(
                {
                    "name": str(name),
                    "value": value,
                    "unit": unit,
                    "nutrient_name": str(name),
                    "amount_per_serving": value,
                    "unit_name": unit,
                }
            )
        return normalized

    return []


def _extract_nutrient_value(total_nutrients: object, names: list[str]) -> float | None:
    candidates = {name.lower() for name in names}
    alias_by_name = {
        "energy": {"energy_kcal", "kcal"},
        "energy (kcal)": {"energy_kcal", "kcal"},
        "energy, kcal": {"energy_kcal", "kcal"},
        "protein": {"protein_g"},
        "carbohydrate": {"carbohydrate_g", "carbs_g"},
        "carbohydrate, by difference": {"carbohydrate_g", "carbs_g"},
        "carbohydrate, by diff.": {"carbohydrate_g", "carbs_g"},
        "total lipid (fat)": {"fat_g"},
        "fat": {"fat_g"},
        "total fat": {"fat_g"},
        "fiber, total dietary": {"fibre_g", "fiber_g"},
        "dietary fiber": {"fibre_g", "fiber_g"},
        "fiber": {"fibre_g", "fiber_g"},
        "sugars, total": {"sugar_g"},
        "sugars, total including nlea": {"sugar_g"},
        "sugars, total nlea": {"sugar_g"},
        "sodium, na": {"sodium_mg"},
        "sodium": {"sodium_mg"},
        "cholesterol": {"cholesterol_mg"},
    }
    flat_candidates = set(candidates)
    for name in candidates:
        flat_candidates.update(alias_by_name.get(name, set()))

    if isinstance(total_nutrients, dict):
        for key, value in total_nutrients.items():
            if str(key).strip().lower() in flat_candidates:
                parsed = _coerce_float(value)
                if parsed is not None:
                    return parsed

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

    if isinstance(nutri_score, str):
        text = nutri_score.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return _coerce_nutri_score(parsed)

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


def _coerce_nutri_score_payload(nutri_score: object) -> dict[str, Any] | None:
    if isinstance(nutri_score, dict):
        return nutri_score
    if isinstance(nutri_score, str):
        text = nutri_score.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except Exception:
                return None
            if isinstance(parsed, dict):
                return parsed
    return None


def _build_nutri_score_breakdown(
    total_nutrients: object,
    profile_details: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(total_nutrients, dict):
        return None

    def _pick(*keys: str) -> float | None:
        for key in keys:
            parsed = _coerce_float(total_nutrients.get(key))
            if parsed is not None:
                return parsed
        return None

    total_energy_kcal = _pick("energy_kcal")
    total_sugar_g = _pick("sugar_g")
    total_sat_fat_g = _pick("saturated_fat_g")
    total_sodium_mg = _pick("sodium_mg")
    total_fiber_g = _pick("fibre_g", "fiber_g")
    total_protein_g = _pick("protein_g")

    required = [
        total_energy_kcal,
        total_sugar_g,
        total_sat_fat_g,
        total_sodium_mg,
        total_fiber_g,
        total_protein_g,
    ]
    if any(value is None for value in required):
        return None

    total_weight_g = 0.0
    fvl_ingredients: list[dict[str, Any]] = []
    for row in profile_details:
        weight = _coerce_float(row.get("weight_g"))
        if weight is None or weight <= 0:
            continue
        total_weight_g += weight
        canonical_food_id = _as_id(row.get("canonical_food_id"))
        ingredient_name = row.get("ingredient") or ""
        usda_id: str | None = None
        if canonical_food_id and canonical_food_id[:2].isdigit():
            usda_id = canonical_food_id
        elif ingredient_name:
            try:
                candidates = query_usda_nutrition_candidates(ingredient_name)
                if candidates and candidates[0].get("distance", 1.0) < _USDA_MATCH_THRESHOLD:
                    usda_id = candidates[0].get("metadata", {}).get("usda_id")
            except Exception:
                pass
        if usda_id:
            fvl_ingredients.append(
                {
                    "name": ingredient_name,
                    "weight_grams": weight,
                    "usda_id": usda_id,
                }
            )

    if total_weight_g <= 0:
        return None

    nutrient_values = {
        "energy": (float(total_energy_kcal) * 4.184 / total_weight_g) * 100.0,
        "sugar": (float(total_sugar_g) / total_weight_g) * 100.0,
        "saturated_fats": (float(total_sat_fat_g) / total_weight_g) * 100.0,
        "sodium": (float(total_sodium_mg) / total_weight_g) * 100.0,
        "fibers": (float(total_fiber_g) / total_weight_g) * 100.0,
        "proteins": (float(total_protein_g) / total_weight_g) * 100.0,
        "fruit_percentage": (
            fruits_veg_legumes_percent(fvl_ingredients) if fvl_ingredients else 0.0
        ),
    }

    breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
    breakdown["inputs"] = {
        "total_weight_g": total_weight_g,
        "ingredients_with_usda_id_count": len(fvl_ingredients),
    }
    return breakdown


@router.get(
    "/autocomplete",
    response_model=None,
    tags=["recipes"],
    summary="Autocomplete recipe titles from Elasticsearch",
)
def recipe_autocomplete(
    q: str = Query("", min_length=0, max_length=120),
    limit: int = Query(8, ge=1, le=20),
) -> dict[str, Any]:
    query = q.strip()
    if len(query) < 2:
        return {"suggestions": {}}

    settings = get_settings()
    search_payload = {
        "size": limit,
        "_source": ["id", "title"],
        "query": {
            "multi_match": {
                "query": query,
                "type": "bool_prefix",
                "fields": ["title", "title._2gram", "title._3gram"],
            }
        },
    }

    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    try:
        response = requests.post(
            url,
            json=search_payload,
            timeout=settings.elastic_timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise map_dependency_error("Elasticsearch", exc) from exc

    hits = payload.get("hits", {}).get("hits", [])
    suggestions: dict[str, str] = {}
    seen: set[str] = set()
    for hit in hits:
        source = hit.get("_source", {})
        title = source.get("title")
        if not isinstance(title, str):
            continue
        normalized = title.strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        rid = _as_id(source.get("id")) or _as_id(hit.get("_id"))
        if not rid:
            continue
        seen.add(key)
        suggestions[rid] = normalized

    return {"suggestions": suggestions}


@router.get(
    "/{recipe_id}",
    response_model=None,
    tags=["recipes"],
    summary="Retrieve a recipe with full metadata by id",
)
def get_recipe(
    recipe_id: str,
    region: str | None = Query(
        default=None,
        description="Optional nutrition region selector: US, IE, or HU.",
    ),
    slim: bool = Query(
        default=False,
        description="When true, return only card-level fields (no nutrition data).",
    ),
) -> RecipeDetailResponse | RecipeCardResponse:
    detail_cache_variant = _recipe_response_cache_variant(region, slim)
    cached_response = _cached_recipe_response(recipe_id, detail_cache_variant, slim)
    if cached_response is not None:
        return cached_response

    recipe = cache_get(recipe_id, variant=_RECIPE_BASE_CACHE_VARIANT)
    if recipe is None:
        try:
            recipe = fetch_recipe_info_by_id(recipe_id)
        except Exception as exc:  # noqa: BLE001
            raise map_dependency_error("Neo4j", exc) from exc

        if not recipe:
            raise NotFoundError("Recipe not found")

        cache_set(recipe_id, recipe, variant=_RECIPE_BASE_CACHE_VARIANT)

    # A request can match either r.recipe_id or r.id. Nutrition/profile stores are keyed by
    # canonical recipe_id, so prefer the resolved recipe_id from Neo4j when available.
    resolved_recipe_id = str(recipe.get("recipe_id") or recipe_id)
    recipe["recipe_id"] = resolved_recipe_id
    if resolved_recipe_id != recipe_id:
        cache_set(resolved_recipe_id, recipe, variant=_RECIPE_BASE_CACHE_VARIANT)
        cached_response = _cached_recipe_response(
            resolved_recipe_id,
            detail_cache_variant,
            slim,
        )
        if cached_response is not None:
            cache_set(
                recipe_id,
                cached_response.model_dump(mode="json"),
                variant=detail_cache_variant,
            )
            return cached_response

    if slim:
        nutri_score_str = recipe.get("nutri_score")
        response = RecipeCardResponse(
            recipe_id=resolved_recipe_id,
            title=recipe.get("title"),
            source=recipe.get("source"),
            source_id=recipe.get("source_id"),
            expert_recipe=bool(recipe.get("expert_recipe", False)),
            image_url=recipe.get("image_url"),
            duration=recipe.get("duration"),
            serves=recipe.get("serves"),
            tags=recipe.get("tags") or [],
            nutri_score_label=nutri_score_str if isinstance(nutri_score_str, str) else None,
            nutri_score_color=_nutri_color_from_score(nutri_score_str),
        )
        _cache_recipe_response(recipe_id, resolved_recipe_id, detail_cache_variant, response)
        return response

    preferred_nutrition_source = _nutrition_source_from_region(region)

    nutrition = None
    try:
        nutrition = get_recipe_nutrition(
            resolved_recipe_id,
            nutrition_source=preferred_nutrition_source,
        )
        if not nutrition:
            nutrition = get_recipe_nutrition(resolved_recipe_id)
    except Exception:
        nutrition = None

    stored_trace = None
    try:
        stored_trace = get_recipe_profile_trace(
            resolved_recipe_id,
            nutrition_source=preferred_nutrition_source,
        )
        if not stored_trace:
            stored_trace = get_recipe_profile_trace(resolved_recipe_id)
    except Exception:
        stored_trace = None

    # On-the-fly profiling for recipes with no stored trace (e.g. unprofiled recipe1m)
    if not stored_trace and not nutrition:
        try:
            ingredients = recipe.get("ingredients") or []
            ingredient_lines = [
                f"{ing.get('measurement', '')} {ing.get('name', '')}".strip()
                if isinstance(ing, dict) else str(ing)
                for ing in ingredients
            ]
            ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
            instructions = recipe.get("instructions") or []
            serves = float(recipe.get("serves") or 4)
            live_region = region or "IE"
            live_result = Recipe_Profiling_Chain_Structured.invoke({
                "title": recipe.get("title", ""),
                "ingredient_names": ingredient_names,
                "measurements": measurements,
                "serves": serves,
                "total_time": recipe.get("duration"),
                "directions": instructions,
                "region": live_region,
                "debug": False,
            })
            if isinstance(live_result, dict):
                stored_trace = live_result
                stored_trace["_computed_on_the_fly"] = True
                ns = live_result.get("nutrition_source_key") or _nutrition_source_from_region(live_region)
                suffix = f"_{ns}"
                from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals
                totals = live_result.get("profiling_totals") or {}
                clean_totals = _extract_clean_totals(totals, suffix)
                clean_per_serving = {k: v / serves for k, v in clean_totals.items()} if clean_totals else None
                nutrition = {
                    "total_nutrients": clean_totals,
                    "total_nutrients_per_serving": clean_per_serving,
                    "nutri_score": live_result.get("nutri_score"),
                    "nutrition_source": live_result.get("nutrition_source") or ns,
                }
                stored_trace["total_nutrients"] = clean_totals
                stored_trace["total_nutrients_per_serving"] = clean_per_serving
        except Exception:
            pass

    if not nutrition and isinstance(stored_trace, dict):
        trace_totals = _as_dict(stored_trace.get("total_nutrients"))
        trace_per_serving = _as_dict(stored_trace.get("total_nutrients_per_serving"))
        if trace_totals or trace_per_serving:
            nutrition = {
                "total_nutrients": trace_totals,
                "total_nutrients_per_serving": trace_per_serving,
                "nutri_score": stored_trace.get("nutri_score"),
                "source": stored_trace.get("source"),
                "nutrition_source": stored_trace.get("nutrition_source"),
            }

    payload = dict(recipe)
    profile_details = _as_list_of_dicts(
        stored_trace.get("nutrition_profiling_details") if isinstance(stored_trace, dict) else None
    )
    profile_debug = _as_dict(
        stored_trace.get("nutrition_profiling_debug") if isinstance(stored_trace, dict) else None
    )

    if profile_details:
        payload["nutrition_profiling_details"] = profile_details
    if profile_debug:
        payload["nutrition_profiling_debug"] = profile_debug

    if nutrition:
        nutri_score_payload = _coerce_nutri_score_payload(nutrition.get("nutri_score"))
        total_nutrients = _as_dict(nutrition.get("total_nutrients"))
        total_nutrients_per_serving = _as_dict(nutrition.get("total_nutrients_per_serving"))
        nutrient_basis = (
            total_nutrients_per_serving
            if isinstance(total_nutrients_per_serving, dict)
            else total_nutrients
        )
        serves = payload.get("serves")
        payload.update(
            {
                "total_kcal_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Energy", "Energy (kcal)", "Energy, kcal"],
                ),
                "total_protein_g_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Protein"],
                ),
                "total_carbs_g_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    [
                        "Carbohydrate",
                        "Carbohydrate, by difference",
                        "Carbohydrate, by diff.",
                    ],
                ),
                "total_fat_g_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Total lipid (fat)", "Fat", "Total fat"],
                ),
                "total_fiber_g_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Fiber, total dietary", "Dietary Fiber", "Fiber"],
                ),
                "total_sugar_g_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    [
                        "Sugars, total",
                        "Sugars, total including NLEA",
                        "Sugars, total NLEA",
                    ],
                ),
                "total_sodium_mg_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Sodium, Na", "Sodium"],
                ),
                "total_cholesterol_mg_per_serving": _extract_nutrient_value(
                    nutrient_basis,
                    ["Cholesterol"],
                ),
                "nutri_score": _coerce_nutri_score(nutrition.get("nutri_score")),
                "nutri_score_label": (
                    nutri_score_payload.get("nutri_score")
                    if isinstance(nutri_score_payload.get("nutri_score"), str)
                    else None
                ) if nutri_score_payload else None,
                "nutri_score_color": (
                    nutri_score_payload.get("color")
                    if isinstance(nutri_score_payload.get("color"), str)
                    else None
                ) if nutri_score_payload else None,
                "total_nutrients": total_nutrients,
                "total_nutrients_per_serving": total_nutrients_per_serving,
                "nutri_score_breakdown": (
                    (stored_trace or {}).get("nutri_score_breakdown")
                    if isinstance((stored_trace or {}).get("nutri_score_breakdown"), dict)
                    else _build_nutri_score_breakdown(total_nutrients, profile_details)
                ),
                "nutrition_source": (
                    nutrition.get("nutrition_source")
                    or nutrition.get("source")
                    or (stored_trace or {}).get("nutrition_source")
                    or (stored_trace or {}).get("source")
                ),
            }
        )
        if not isinstance(total_nutrients_per_serving, dict):
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

    response = RecipeDetailResponse(**payload)
    _cache_recipe_response(recipe_id, resolved_recipe_id, detail_cache_variant, response)
    return response


def _resolve_profile_recipe_id(payload: dict[str, Any], profile_result: dict[str, Any]) -> str | None:
    explicit = _as_id(payload.get("recipe_id"))
    if explicit:
        return explicit

    title = str(profile_result.get("title") or "").strip()
    if not title:
        return None
    try:
        info = fetch_recipe_info(recipe_title=title)
    except Exception:
        return None
    return _as_id((info or {}).get("recipe_id"))


def _persist_profile_trace_best_effort(payload: dict[str, Any], profile_result: dict[str, Any]) -> tuple[bool, str | None]:
    recipe_id = _resolve_profile_recipe_id(payload, profile_result)
    if not recipe_id:
        return False, "Could not resolve recipe_id for trace persistence."

    totals = profile_result.get("profiling_totals")
    profile_pipeline_version = _profile_meta()

    # Normalize to clean keys for consistent postgres storage
    from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals, _CLEAN_TOTAL_KEYS
    nutrition_source_key = profile_result.get("nutrition_source_key") or ""
    suffix = f"_{nutrition_source_key}" if nutrition_source_key else ""
    clean_totals = _extract_clean_totals(totals, suffix) if isinstance(totals, dict) else None
    clean_per_serving = (
        {k: v / profile_result.get("serves", 1) for k, v in clean_totals.items()}
        if clean_totals and profile_result.get("serves")
        else None
    )

    trace_payload = {
        "recipe_id": recipe_id,
        "title": profile_result.get("title"),
        "source": profile_result.get("source"),
        "nutrition_source": profile_result.get("nutrition_source"),
        "total_nutrients": clean_totals,
        "total_nutrients_per_serving": clean_per_serving,
        "nutri_score": profile_result.get("nutri_score"),
        "nutri_score_breakdown": profile_result.get("nutri_score_breakdown"),
        "nutrition_profiling_details": profile_result.get("ingredients"),
        "nutrition_profiling_debug": profile_result.get("pipeline_trace"),
        "trace": profile_result,
        "pipeline_version": profile_pipeline_version,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_recipe_profile_trace(trace_payload)
    cache_delete(recipe_id)
    return True, None


@router.post(
    "/search",
    response_model=None,
    tags=["recipes"],
    summary="Search recipes via the knowledge graph",
)
async def recipe_search(
    payload: RecipeSearchRequest,
) -> dict[str, Any]:
    """Invoke the recipe search LangGraph pipeline and return its output."""

    question = str(payload.question or "").strip()
    exclude_allergens = payload.exclude_allergens if isinstance(payload.exclude_allergens, list) else []

    # If no free-text question is provided, return a random landing page.
    if not question:
        random_results: list[dict[str, Any]] = []
        try:
            random_results = _random_myplate_from_elastic(limit=10)
        except Exception:  # noqa: BLE001
            random_results = []

        return {"results": random_results or []}

    # Lazily initialize Neo/Groq search stack only for non-empty queries.
    recipe_search_app = get_recipe_search_app()

    try:
        result = recipe_search_app.invoke(question, exclude_allergens)
    except Exception as exc:  # noqa: BLE001 - bubble up as HTTP error
        # Keep the endpoint usable even if the primary graph/LLM path fails.
        try:
            fallback_results = search_recipes_by_params(
                RecipeSearchFilters(
                    exclude_allergens=exclude_allergens,
                    limit=10,
                )
            )
        except Exception:
            fallback_results = _random_myplate_from_elastic(limit=10)

        if not isinstance(fallback_results, list):
            fallback_results = []

        fallback_results = _normalize_search_results(fallback_results)
        return {
            "results": fallback_results or [],
            "warning": "Primary search path failed; returned fallback results.",
            "error": str(exc),
        }
    if not isinstance(result, dict):
        raise InternalError(
            detail="Recipe search returned unexpected payload",
            extra={"title": "SearchPipelineError"},
        )

    raw_results = result.get("results", [])
    if isinstance(raw_results, list):
        raw_results = _normalize_search_results(raw_results)

    return {"results": raw_results}


@router.post(
    "/param_search",
    response_model=None,
    tags=["recipes"],
    summary="Build Cypher for deterministic parameter-based recipe search",
)
def param_search(payload: RecipeSearchFilters) -> dict[str, Any]:
    """Run deterministic parameter-based recipe search and return results."""

    try:
        results = search_recipes_by_params(payload)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Neo4j", exc) from exc

    cards = []
    for row in results:
        if not isinstance(row, dict):
            cards.append(row)
            continue
        nutri_score = row.get("nutri_score")
        cards.append({
            "recipe_id": row.get("recipe_id"),
            "title": row.get("title"),
            "source": row.get("source"),
            "source_id": row.get("source_id"),
            "image_url": row.get("image_url"),
            "duration": row.get("duration"),
            "serves": row.get("serves"),
            "nutri_score": nutri_score,
            "nutri_score_color": _nutri_color_from_score(nutri_score),
            "sust_score": row.get("sust_score"),
            "expert_recipe": row.get("expert_recipe", False),
        })
    return {"results": cards}


@router.post(
    "/profile",
    response_model=None,
    tags=["recipes"],
    summary="Run parsing + profiling pipeline on raw recipe text",
)
async def recipe_profile(
    payload: RecipeProfileRequest,
) -> Any:
    """Execute recipe profiling on raw recipe text."""
    raw_recipe = str(payload.raw_recipe or "").strip()
    region = str(payload.region or "IE").strip().upper()

    if region not in {"IE", "US", "HU"}:
        region = "US"

    if payload.parse_only:
        from recipe_wrangler.tools.parse_recipe_tool import parse_recipe_tool
        try:
            parsed = parse_recipe_tool.invoke({"recipe": raw_recipe})
        except Exception as exc:
            raise map_dependency_error("Parse pipeline", exc) from exc
        names = parsed.get("ingredient_names") or []
        measurements = parsed.get("measurements") or []
        ingredients = [
            f"{m} {n}".strip() if m else n
            for n, m in zip(names, measurements)
        ]
        total_time = parsed.get("total_time") or 0
        serves = parsed.get("serves") or 0
        try:
            auto_allergens = detect_allergens_from_names(names)
            auto_tags = list(infer_diet_tags(set(auto_allergens)))
        except Exception:
            auto_allergens, auto_tags = [], []
        return {
            "message": "Success",
            # fields matching RecipeCreateRequest directly
            "title": parsed.get("title"),
            "ingredients": ingredients,
            "instructions": parsed.get("directions") or [],
            "duration": total_time if total_time > 0 else None,
            "serves": serves if serves > 0 else None,
            "allergens": auto_allergens,
            "tags": auto_tags,
            # also expose split form for display/editing
            "ingredient_names": names,
            "measurements": measurements,
            "directions": parsed.get("directions") or [],
            "total_time": total_time if total_time > 0 else None,
        }

    try:
        profile_result = Recipe_Profiling_Chain.invoke(
            {"recipe_text": raw_recipe, "debug": False, "region": region}
        )
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Profiling pipeline", exc) from exc

    if not isinstance(profile_result, dict):
        raise InternalError(
            detail="Recipe profiling returned unexpected payload",
            extra={"title": "ProfilingPipelineError"},
        )

    payload_dict = payload.model_dump()
    persist_trace = bool(payload.persist_trace)
    if bool(persist_trace):
        try:
            persisted, warning = _persist_profile_trace_best_effort(payload_dict, profile_result)
            if warning:
                profile_result["profiling_trace_warning"] = warning
            profile_result["profiling_trace_persisted"] = bool(persisted)
        except Exception as exc:  # noqa: BLE001
            profile_result["profiling_trace_persisted"] = False
            profile_result["profiling_trace_warning"] = f"Failed to persist trace: {exc}"

    # Return the full chain output so clients can access all parsed/profiling fields.
    # Strip top-level None values — they represent unset pipeline state, not meaningful nulls.
    profile_result = {k: v for k, v in profile_result.items() if v is not None}
    return {"message": "Success", **profile_result}


# ---------------------------------------------------------------------------
# Recipe creation endpoint
# ---------------------------------------------------------------------------

def _generate_user_recipe_id(title: str, ingredients: list[str]) -> str:
    """Generate a UUID for a newly created user recipe."""
    _ = (title, ingredients)  # keep signature compatibility for existing call sites
    return str(uuid4())


def _index_recipe_to_elastic(
    recipe_id: str,
    title: str,
    ingredient_names: list[str],
    tags: list[str],
) -> None:
    """Index a single recipe document into Elasticsearch (best-effort)."""
    settings = get_settings()
    url = f"{settings.elastic_url}/{settings.elastic_index}/_doc/{recipe_id}"
    doc = {
        "id": recipe_id,
        "title": title,
        "ingredients": ingredient_names,
        "tags": tags,
    }
    requests.put(url, json=doc, timeout=settings.elastic_timeout)


@router.post(
    "/create",
    response_model=RecipeCreateResponse,
    include_in_schema=False,
    deprecated=True,
)
@router.post(
    "/",
    response_model=RecipeCreateResponse,
    tags=["recipes"],
    summary="Create a new user recipe with nutrition profiling",
)
async def recipe_create(payload: RecipeCreateRequest) -> RecipeCreateResponse:
    """Create a new recipe from structured fields.

    1. Splits raw ingredient strings into names + measurements.
    2. Either uses provided total nutrient values (if complete), or runs weight estimation + profiling.
    3. Auto-detects allergens from ingredient names; merges with user-supplied ones.
    4. Infers diet tags from allergens; merges with user-supplied tags.
    5. Writes the recipe and its ingredient/allergen/tag graph to Neo4j.
    6. Persists the nutrition profile trace to Postgres.
    7. Indexes the recipe in Elasticsearch for search/autocomplete.
    """
    region = str(payload.region or "IE").strip().upper()
    ingredient_names, measurements = split_ingredient_lines(payload.ingredients)
    recipe_id = _generate_user_recipe_id(payload.title, payload.ingredients)
    nutrition_source = _nutrition_source_from_region(region) or "usda"

    manual_nutrients: dict[str, float | None] = {
        "protein_g": payload.protein_g,
        "carbohydrate_g": payload.carbohydrate_g,
        "fat_g": payload.fat_g,
        "energy_kcal": payload.energy_kcal,
        "sugar_g": payload.sugar_g,
        "saturated_fat_g": payload.saturated_fat_g,
        "sodium_mg": payload.sodium_mg,
        "fibre_g": payload.fibre_g,
    }
    provided_manual_count = sum(1 for v in manual_nutrients.values() if v is not None)
    has_manual_nutrients = provided_manual_count == len(manual_nutrients)
    has_partial_manual_nutrients = 0 < provided_manual_count < len(manual_nutrients)

    if has_partial_manual_nutrients:
        raise DataError(
            detail=(
                "Manual nutrients must include all fields or none: "
                "protein_g, carbohydrate_g, fat_g, energy_kcal, sugar_g, "
                "saturated_fat_g, sodium_mg, fibre_g."
            ),
            extra={"title": "IncompleteManualNutrients"},
        )

    profile_result: dict[str, Any] | None = None
    clean_totals: dict[str, float] | None = None
    clean_per_serving: dict[str, float] | None = None
    nutri_score_breakdown: dict[str, Any] | None = None

    if has_manual_nutrients:
        clean_totals = {k: float(v) for k, v in manual_nutrients.items() if v is not None}
        clean_per_serving = {
            k: v / payload.serves for k, v in clean_totals.items()
        }
    else:
        # --- Nutrition profiling ---
        try:
            profile_result = Recipe_Profiling_Chain_Structured.invoke({
                "title": payload.title,
                "ingredient_names": ingredient_names,
                "measurements": measurements,
                "serves": float(payload.serves),
                "total_time": float(payload.duration),
                "directions": payload.instructions,
                "region": region,
                "debug": False,
            })
        except Exception as exc:
            raise map_dependency_error("Profiling pipeline", exc) from exc

        if not isinstance(profile_result, dict):
            raise InternalError(
                detail="Profiling pipeline returned unexpected payload",
                extra={"title": "ProfilingPipelineError"},
            )

        from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals, _resolve_fvl_usda_id
        from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent

        nutrition_source_key = profile_result.get("nutrition_source_key") or nutrition_source
        totals = profile_result.get("profiling_totals") or {}
        clean_totals = _extract_clean_totals(totals, f"_{nutrition_source_key}")
        clean_per_serving = (
            {k: v / payload.serves for k, v in clean_totals.items()}
            if clean_totals else None
        )

        # Compute nutri_score_breakdown immediately (same logic as backfill)
        if clean_totals:
            try:
                prof_ingredients = profile_result.get("ingredients") or []
                score_ingredients = []
                total_weight = 0.0
                for ing in prof_ingredients:
                    if not isinstance(ing, dict):
                        continue
                    w = ing.get("weight_g") or ing.get("weight_grams")
                    if not w:
                        continue
                    total_weight += float(w)
                    usda_id = _resolve_fvl_usda_id(ing.get("canonical_food_id"), ing.get("name"))
                    entry = {"name": ing.get("name"), "weight_grams": float(w)}
                    if usda_id:
                        entry["usda_id"] = usda_id
                    score_ingredients.append(entry)
                fvl_pct = fruits_veg_legumes_percent(score_ingredients) if score_ingredients else 0.0
                nutri_score_breakdown = compute_nutri_score_breakdown_from_values(
                    protein_g=clean_totals["protein_g"],
                    carbohydrate_g=clean_totals["carbohydrate_g"],
                    fat_g=clean_totals["fat_g"],
                    energy_kcal=clean_totals["energy_kcal"],
                    sugar_g=clean_totals["sugar_g"],
                    saturated_fat_g=clean_totals["saturated_fat_g"],
                    sodium_mg=clean_totals["sodium_mg"],
                    fibre_g=clean_totals["fibre_g"],
                    fvl_percent=fvl_pct,
                    total_weight_g=total_weight,
                    ingredients_with_usda_id_count=sum(1 for e in score_ingredients if "usda_id" in e),
                )
            except Exception:
                pass

    # --- Allergen + tag resolution ---
    auto_allergens = detect_allergens_from_names(ingredient_names)
    merged_allergens: list[str] = sorted(set(auto_allergens) | set(payload.allergens))
    auto_tags = infer_diet_tags(set(merged_allergens))
    merged_tags: list[str] = sorted(set(auto_tags) | set(payload.tags))

    # --- Neo4j write ---
    try:
        upsert_recipe_to_neo4j(
            recipe_id=recipe_id,
            title=payload.title,
            ingredient_lines=payload.ingredients,
            ingredient_names=ingredient_names,
            measurements=measurements,
            instructions=payload.instructions,
            duration=float(payload.duration),
            serves=float(payload.serves),
            image_url=payload.image_url,
            allergens=merged_allergens,
            tags=merged_tags,
            source="user",
            source_id=payload.source_id,
            expert_recipe=payload.expert_recipe,
        )
    except Exception as exc:
        raise map_dependency_error("Neo4j", exc) from exc

    trace_payload: dict[str, Any] = {
        "recipe_id": recipe_id,
        "title": payload.title,
        "source": "user",
        "nutrition_source": (
            (profile_result.get("nutrition_source") if profile_result else None) or nutrition_source
        ),
        "total_nutrients": clean_totals,
        "total_nutrients_per_serving": clean_per_serving,
        "nutri_score": profile_result.get("nutri_score") if profile_result else None,
        "nutri_score_breakdown": nutri_score_breakdown,
        "nutrition_profiling_details": profile_result.get("ingredients") if profile_result else None,
        "nutrition_profiling_debug": (
            profile_result.get("pipeline_trace")
            if profile_result
            else {"profiling_skipped": True, "mode": "manual_nutrients"}
        ),
        "trace": (
            {"profile_result": profile_result}
            if profile_result
            else {"profiling_skipped": True, "manual_total_nutrients": clean_totals}
        ),
        "pipeline_version": _profile_meta(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace
        upsert_recipe_profiling_trace(trace_payload)
        cache_delete(recipe_id)
    except Exception:
        pass  # non-fatal — recipe is in Neo4j, postgres trace is best-effort

    # --- Elasticsearch index ---
    try:
        _index_recipe_to_elastic(recipe_id, payload.title, ingredient_names, merged_tags)
    except Exception:
        pass  # non-fatal

    return RecipeCreateResponse(recipe_id=recipe_id)


# ---------------------------------------------------------------------------
# Ingredient substitution endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/{recipe_id}/substitute",
    response_model=RecipeSubstituteResponse,
    tags=["recipes"],
    summary="Substitute an ingredient and return the updated nutrition profile",
)
async def recipe_substitute(
    recipe_id: str,
    payload: RecipeSubstituteRequest,
) -> RecipeSubstituteResponse:
    """Find the best substitute for an ingredient in a recipe and re-profile the result.

    Lookup order for substitutes:
    1. HAS_SUBSTITUTION edges (MISKG-curated).
    2. FoodOn taxonomy siblings (3-hop ancestor search).

    Returns the first (best) candidate along with the full nutrition profile
    of the recipe after the swap.
    """
    region = str(payload.region or "IE").strip().upper()
    if region not in {"IE", "US", "HU"}:
        region = "IE"

    # --- Fetch recipe ---
    try:
        recipe = fetch_recipe_info_by_id(recipe_id)
    except Exception as exc:
        raise map_dependency_error("Neo4j", exc) from exc

    if not recipe:
        raise NotFoundError(detail=f"Recipe '{recipe_id}' not found")

    # --- Confirm ingredient is in the recipe ---
    ingredient_lower = payload.ingredient.strip().lower()
    recipe_ingredients: list[dict[str, Any]] = recipe.get("ingredients") or []
    matched = next(
        (ing for ing in recipe_ingredients if (ing.get("name") or "").lower() == ingredient_lower),
        None,
    )
    if matched is None:
        raise NotFoundError(
            detail=f"Ingredient '{payload.ingredient}' not found in recipe '{recipe_id}'"
        )

    # --- Find substitutes ---
    try:
        sub_result = find_ingredient_substitutes(payload.ingredient)
    except Exception as exc:
        raise map_dependency_error("Neo4j", exc) from exc

    candidates: list[str] = sub_result.get("candidates") or []
    source: str | None = sub_result.get("source")

    if not candidates:
        raise NotFoundError(
            detail=f"No substitutes found for ingredient '{payload.ingredient}'"
        )

    best_substitute = candidates[0]

    # --- Build modified ingredient list (swap name, keep measurement) ---
    modified_ingredient_names: list[str] = []
    modified_measurements: list[str] = []
    for ing in recipe_ingredients:
        name = ing.get("name") or ""
        measurement = ing.get("measurement") or ing.get("quantity") or name
        if name.lower() == ingredient_lower:
            modified_ingredient_names.append(best_substitute)
        else:
            modified_ingredient_names.append(name)
        modified_measurements.append(measurement)

    # --- Re-profile with substitute ---
    serves = float(recipe.get("serves") or 1)
    total_time = recipe.get("duration")

    try:
        profile_result = Recipe_Profiling_Chain_Structured.invoke({
            "title": recipe.get("title") or "",
            "ingredient_names": modified_ingredient_names,
            "measurements": modified_measurements,
            "serves": serves,
            "total_time": float(total_time) if total_time is not None else None,
            "directions": recipe.get("instructions") or [],
            "region": region,
            "debug": False,
        })
    except Exception as exc:
        raise map_dependency_error("Profiling pipeline", exc) from exc

    if not isinstance(profile_result, dict):
        raise InternalError(
            detail="Profiling pipeline returned unexpected payload",
            extra={"title": "ProfilingPipelineError"},
        )

    # Strip top-level None values (unset pipeline state)
    profile_result = {k: v for k, v in profile_result.items() if v is not None}

    return RecipeSubstituteResponse(
        original_ingredient=payload.ingredient,
        substitute=best_substitute,
        substitution_source=source,
        candidates=candidates,
        modified_recipe_profile=profile_result,
    )


@router.patch(
    "/{recipe_id}",
    response_model=RecipeUpdateResponse,
    tags=["recipes"],
    summary="Update recipe instructions and/or image URL across all stores",
)
async def recipe_update(recipe_id: str, payload: RecipeUpdateRequest) -> RecipeUpdateResponse:
    """Patch mutable fields on an existing recipe.

    - **Neo4j**: updates ``instructions`` and/or ``image_url`` on the Recipe node.
    - **Elasticsearch**: updates the indexed document if ``image_url`` changes
      (instructions are not indexed).
    - **Postgres**: nutrition traces are not affected (they store nutrients, not content).

    Returns 404 if the recipe does not exist in Neo4j.
    """
    patchable = ("instructions", "image_url", "source_id", "expert_recipe", "title", "allergens", "tags", "duration")
    if all(getattr(payload, f) is None for f in patchable):
        raise NotFoundError(detail="No fields provided to update")

    updated_fields = [f for f in patchable if getattr(payload, f) is not None]

    # --- Neo4j ---
    try:
        found = update_recipe_in_neo4j(
            recipe_id=recipe_id,
            instructions=payload.instructions,
            image_url=payload.image_url,
            source_id=payload.source_id,
            expert_recipe=payload.expert_recipe,
            title=payload.title,
            allergens=payload.allergens,
            tags=payload.tags,
            duration=payload.duration,
        )
    except Exception as exc:
        raise map_dependency_error("Neo4j", exc) from exc

    if not found:
        raise NotFoundError(detail=f"Recipe {recipe_id} not found")

    cache_delete(recipe_id)
    try:
        resolved_rows = _run_query(
            """
            MATCH (r:Recipe)
            WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
            RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id
            LIMIT 1
            """,
            {"recipe_id": recipe_id},
        )
        resolved_cache_id = (
            _as_id(resolved_rows[0].get("recipe_id"))
            if resolved_rows else None
        )
        if resolved_cache_id and resolved_cache_id != recipe_id:
            cache_delete(resolved_cache_id)
    except Exception:
        pass

    # --- Elasticsearch (image_url only — instructions are not indexed) ---
    if payload.image_url is not None:
        try:
            settings = get_settings()
            url = f"{settings.elastic_url}/{settings.elastic_index}/_update/{recipe_id}"
            requests.post(
                url,
                json={"doc": {"image_url": payload.image_url}},
                timeout=settings.elastic_timeout,
            )
        except Exception:
            pass  # non-fatal

    current_tags: list[str] = []
    try:
        tag_rows = _run_query(
            """
            MATCH (r:Recipe)-[:HAS_TAG]->(t:Tag)
            WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
            RETURN t.name AS name
            """,
            {"recipe_id": recipe_id},
        )
        current_tags = [row["name"] for row in tag_rows if row.get("name")]
    except Exception:
        pass

    current_allergens: list[str] = []
    try:
        allergen_rows = _run_query(
            """
            MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
            WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
            RETURN DISTINCT al.name AS name
            """,
            {"recipe_id": recipe_id},
        )
        current_allergens = [row["name"] for row in allergen_rows if row.get("name")]
    except Exception:
        pass

    return RecipeUpdateResponse(recipe_id=recipe_id, updated_fields=updated_fields, tags=current_tags, allergens=current_allergens)
