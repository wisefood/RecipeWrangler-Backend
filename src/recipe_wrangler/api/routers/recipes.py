"""Recipe-related endpoints router."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from fastapi import APIRouter, BackgroundTasks, Query
from starlette.concurrency import run_in_threadpool

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.exceptions import (
    ConflictError,
    DataError,
    InternalError,
    InvalidError,
    NotFoundError,
)
from recipe_wrangler.api.config import get_settings
from recipe_wrangler.utils.http_pool import get_http_session

from recipe_wrangler.tools.param_search import search_recipes_by_params
from recipe_wrangler.tools.es_recipe_search import (
    RecipeSearchConstraints,
    ResultWindowExceededError,
    search_recipes_es,
)
from recipe_wrangler.utils.recipe_cache import (
    cache_delete,
    cache_delete_many,
    cache_get,
    cache_mget,
    cache_mset,
    cache_set,
)
from recipe_wrangler.utils.es_recipe_projection import project_recipe_to_es_v2
from recipe_wrangler.utils.recipe_status import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    es_not_disabled_clause,
    status_job_guard,
    sync_recipe_status_to_es,
)
from recipe_wrangler.utils.neo4j_utils import run_query as _run_query
from recipe_wrangler.tools.fetch_recipe_info import (
    fetch_recipe_info,
    fetch_recipe_info_by_ids,
    fetch_recipe_info_by_id,
)
from recipe_wrangler.repositories.neo4j_recipes import (
    count_recipes,
    fetch_foodchat_candidates,
    detect_allergens_from_names,
    fetch_recipe_allergens_by_ids,
    fetch_recipe_dish_types_by_ids,
    fetch_recipe_image_urls_by_ids,
    fetch_recipe_scores_by_ids,
    find_ingredient_substitutes,
    infer_diet_tags,
    resolve_collection_source_id,
    resolve_recipe_ids_by_query,
    set_recipe_status,
    update_recipe_in_neo4j,
    upsert_recipe_to_neo4j,
)
from recipe_wrangler.repositories.postgres_nutrition import (
    get_recipe_nutrition,
    get_recipe_nutrition_batch,
    get_recipe_profile_trace,
    save_recipe_profile_trace,
)
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent
from recipe_wrangler.repositories.chroma_matchers import query_usda_nutrition_candidates

logger = logging.getLogger(__name__)

_USDA_MATCH_THRESHOLD = 0.4
_REPO_ROOT = Path(__file__).resolve().parents[4]
_HEALTHYFOODS_NUTRITION_PATH = (
    _REPO_ROOT / "data/HealthyFoods/HealthyFood_recipes_nutrition.json"
)
from recipe_wrangler.tools.recipe_profiling_chain import (
    Recipe_Profiling_Chain,
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)

_PROFILE_TIMEOUT_SECONDS = 25.0


async def _invoke_profile_with_timeout(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.wait_for(
        asyncio.to_thread(Recipe_Profiling_Chain_Structured.invoke, payload),
        timeout=_PROFILE_TIMEOUT_SECONDS,
    )

from ..dependencies import get_recipe_search_app
from recipe_wrangler.schemas import (
    FoodChatRequest,
    FoodChatResponse,
    RecipeCardNutrition,
    RecipeCardResponse,
    RecipeBulkStatusRequest,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeDetailResponse,
    RecipeDisableByQueryRequest,
    RecipeDisableRequest,
    RecipeStatusResponse,
    RecipeDetailsBatchRequest,
    RecipeDetailsBatchResponse,
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
    mapping = {"US": "usda", "IE": "irish", "HU": "hungarian", "EU": "eu"}
    return mapping.get(region_norm)


def _recipe_response_cache_variant(region: str | None, slim: bool) -> str:
    region_key = str(region or "default").strip().upper() or "DEFAULT"
    region_key = "".join(ch if ch.isalnum() else "_" for ch in region_key)
    return f"detail:region:{region_key}:slim:{int(slim)}"


def _card_nutrition_cache_variant(region: str | None) -> str:
    region_key = str(region or "default").strip().upper() or "DEFAULT"
    region_key = "".join(ch if ch.isalnum() else "_" for ch in region_key)
    return f"card_nutrition:region:{region_key}"


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
                        ],
                        "must_not": [es_not_disabled_clause()],
                    }
                },
                "random_score": {},
            }
        },
    }
    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    # Keep startup UX snappy; fail fast to local fallback if ES is slow/unavailable.
    response = get_http_session().post(url, json=payload, timeout=min(settings.elastic_timeout, 1.5))
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


def _search_elastic_keyword(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search recipes in Elasticsearch using a multi_match query."""
    settings = get_settings()
    safe_limit = max(1, min(int(limit), 100))
    payload = {
        "size": safe_limit,
        "_source": ["id", "title", "image_url", "source"],
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["title^3", "ingredients^2", "tags"],
                                    "fuzziness": "AUTO",
                                }
                            }
                        ],
                        "must_not": [es_not_disabled_clause()],
                    }
                },
                "random_score": {},
                "boost_mode": "multiply",
            }
        },
    }
    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    response = get_http_session().post(url, json=payload, timeout=settings.elastic_timeout)
    response.raise_for_status()
    body = response.json()
    hits = body.get("hits", {}).get("hits", [])

    results: list[dict[str, Any]] = []
    for hit in hits:
        source = hit.get("_source", {}) if isinstance(hit, dict) else {}
        rid = _as_id(source.get("id")) or _as_id(hit.get("_id"))
        title = source.get("title")
        image_url = source.get("image_url")
        source_name = _as_id(source.get("source"))
        if not rid:
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        results.append(
            {
                "recipe_id": rid,
                "title": title.strip(),
                "source": source_name.casefold() if source_name else None,
                "image_url": image_url.strip() if image_url else None,
            }
        )
    return results


def _normalize_search_results(raw_results: list[object]) -> list[object]:
    """Attach metadata and keep only public keys for search responses."""

    raw_results = _attach_nutri_colors(raw_results)
    raw_results = _attach_recipe_scores(raw_results)
    raw_results = _attach_image_urls(raw_results)
    allowed_keys = {
        "recipe_id",
        "title",
        "url",
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


def _source_ground_truth_nutrition_source(recipe_source: object) -> str | None:
    source = str(recipe_source or "").strip()
    mapping = {
        "Curated Irish Recipes": "safefood_rcsi",
        "SafeFood": "safefood_rcsi",
        "HealthyFoods": "healthyfoods_original",
        "recipe1m": "recipe1m_original",
    }
    return mapping.get(source)


def _source_ground_truth_nutrition_sources(recipe_source: object) -> list[str]:
    primary = _source_ground_truth_nutrition_source(recipe_source)
    if primary == "safefood_rcsi":
        return ["safefood_rcsi", "safefood"]
    if primary == "healthyfoods_original":
        return ["healthyfoods_original", "healthyfoods"]
    return [primary] if primary else []


def _is_source_ground_truth_trace(trace: dict[str, Any] | None) -> bool:
    if not isinstance(trace, dict) or trace.get("_computed_on_the_fly"):
        return False
    nutrition_source = str(trace.get("nutrition_source") or "").strip().lower()
    pipeline_version = str(trace.get("pipeline_version") or "").strip().lower()
    return (
        nutrition_source in {
            "safefood_rcsi",
            "safefood",
            "recipe1m_original",
            "healthyfoods",
            "healthyfoods_original",
        }
        or "ground_truth" in pipeline_version
    )


def _ground_truth_nutrition_payload(
    source_trace: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build source-provided nutrition payload for recipe detail responses."""
    if not _is_source_ground_truth_trace(source_trace):
        return None

    total_nutrients_per_serving = _as_dict(source_trace.get("total_nutrients_per_serving"))
    if not total_nutrients_per_serving:
        return None

    payload: dict[str, Any] = {
        "recipe_source": source_trace.get("source"),
        "nutrition_source": source_trace.get("nutrition_source"),
        "nutrients_per_serving": total_nutrients_per_serving,
        "nutri_score": source_trace.get("nutri_score"),
    }
    for key in (
        "nutri_score_breakdown",
        "computed_at",
        "updated_at",
        "pipeline_version",
    ):
        if source_trace.get(key) is not None:
            payload[key] = source_trace.get(key)
    return payload


def _healthyfoods_nutrition_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return _coerce_float(text.split()[0])


@lru_cache(maxsize=1)
def _healthyfoods_source_nutrition_index() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_HEALTHYFOODS_NUTRITION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    recipes = data.get("recipes") if isinstance(data, dict) else None
    if not isinstance(recipes, list):
        return {}

    index: dict[str, dict[str, Any]] = {}
    for row in recipes:
        if not isinstance(row, dict):
            continue
        for key in (row.get("url"), row.get("title")):
            text_key = str(key or "").strip().lower()
            if text_key:
                index[text_key] = row
    return index


def _healthyfoods_ground_truth_nutrition(recipe: dict[str, Any]) -> dict[str, Any] | None:
    if str(recipe.get("source") or "").strip() != "HealthyFoods":
        return None
    index = _healthyfoods_source_nutrition_index()
    row = None
    for key in (recipe.get("source_id"), recipe.get("url"), recipe.get("title")):
        text_key = str(key or "").strip().lower()
        if text_key and text_key in index:
            row = index[text_key]
            break
    if not row:
        return None

    raw = row.get("nutrition_per_serve")
    if not isinstance(raw, dict):
        return None
    per_serving = {
        "energy_kcal": _healthyfoods_nutrition_number(raw.get("Calories")),
        "energy_kj": _healthyfoods_nutrition_number(raw.get("Kilojoules")),
        "protein_g": _healthyfoods_nutrition_number(raw.get("Protein")),
        "fat_g": _healthyfoods_nutrition_number(raw.get("Total fat")),
        "saturated_fat_g": _healthyfoods_nutrition_number(raw.get("Saturated fat")),
        "carbohydrate_g": _healthyfoods_nutrition_number(raw.get("Carbohydrates")),
        "sugar_g": _healthyfoods_nutrition_number(raw.get("Sugar")),
        "fibre_g": _healthyfoods_nutrition_number(raw.get("Dietary fibre")),
        "sodium_mg": _healthyfoods_nutrition_number(raw.get("Sodium")),
        "calcium_mg": _healthyfoods_nutrition_number(raw.get("Calcium")),
        "iron_mg": _healthyfoods_nutrition_number(raw.get("Iron")),
    }
    per_serving = {k: v for k, v in per_serving.items() if v is not None}
    if not per_serving:
        return None

    return {
        "recipe_source": "HealthyFoods",
        "nutrition_source": "healthyfoods_original",
        "nutrients_per_serving": per_serving,
        "raw_nutrition_per_serving": raw,
        "source_url": row.get("url"),
    }


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
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "type": "bool_prefix",
                            "fields": ["title", "title._2gram", "title._3gram"],
                        }
                    }
                ],
                "must_not": [es_not_disabled_clause()],
            }
        },
    }

    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    try:
        response = get_http_session().post(
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
    "/count",
    response_model=None,
    tags=["recipes"],
    summary="Return the total number of recipes in the graph",
)
def get_recipe_count() -> dict[str, int]:
    try:
        total = count_recipes()
    except Exception as exc:
        raise map_dependency_error("Neo4j", exc) from exc
    return {"count": total}


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
    include_disabled: bool = Query(
        default=False,
        description="Console/admin: also resolve disabled (soft-deleted) recipes.",
    ),
) -> RecipeDetailResponse | RecipeCardResponse:
    # Console reads of potentially-disabled recipes bypass the cache entirely —
    # public cache entries must never be populated from an include_disabled read.
    detail_cache_variant = _recipe_response_cache_variant(region, slim)
    if not include_disabled:
        cached_response = _cached_recipe_response(recipe_id, detail_cache_variant, slim)
        if cached_response is not None:
            return cached_response

    recipe = None if include_disabled else cache_get(recipe_id, variant=_RECIPE_BASE_CACHE_VARIANT)
    if recipe is None:
        try:
            recipe = fetch_recipe_info_by_id(recipe_id, include_disabled=include_disabled)
        except Exception as exc:  # noqa: BLE001
            raise map_dependency_error("Neo4j", exc) from exc

        if not recipe:
            raise NotFoundError("Recipe not found")

        if not include_disabled:
            cache_set(recipe_id, recipe, variant=_RECIPE_BASE_CACHE_VARIANT)

    # A request can match either r.recipe_id or r.id. Nutrition/profile stores are keyed by
    # canonical recipe_id, so prefer the resolved recipe_id from Neo4j when available.
    resolved_recipe_id = str(recipe.get("recipe_id") or recipe_id)
    recipe["recipe_id"] = resolved_recipe_id
    if resolved_recipe_id != recipe_id and not include_disabled:
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
            url=recipe.get("url"),
            source=recipe.get("source"),
            source_id=recipe.get("source_id"),
            expert_recipe=bool(recipe.get("expert_recipe", False)),
            image_url=recipe.get("image_url"),
            duration=recipe.get("duration"),
            serves=recipe.get("serves"),
            cost_category=recipe.get("cost_category"),
            tags=recipe.get("tags") or [],
            nutri_score_label=nutri_score_str if isinstance(nutri_score_str, str) else None,
            nutri_score_color=_nutri_color_from_score(nutri_score_str),
            status=str(recipe.get("status") or "active"),
        )
        if not include_disabled:
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

    source_ground_truth_trace = None
    ground_truth_sources = _source_ground_truth_nutrition_sources(recipe.get("source"))
    for ground_truth_source in ground_truth_sources:
        try:
            source_ground_truth_trace = get_recipe_profile_trace(
                resolved_recipe_id,
                nutrition_source=ground_truth_source,
            )
        except Exception:
            source_ground_truth_trace = None
        if source_ground_truth_trace:
            break
    if source_ground_truth_trace is None and _is_source_ground_truth_trace(stored_trace):
        source_ground_truth_trace = stored_trace

    ground_truth_nutrition = (
        _ground_truth_nutrition_payload(source_ground_truth_trace)
        or _healthyfoods_ground_truth_nutrition(recipe)
    )

    # On-the-fly profiling for recipes with no stored trace (e.g. unprofiled
    # recipe1m). The profiling chain is far too slow to block a GET on, so it
    # runs in a background thread that persists the trace and invalidates the
    # response cache; until then the response carries profiling_status=pending.
    profiling_status = None
    if not stored_trace and not nutrition:
        if _schedule_live_profile_job(resolved_recipe_id, recipe, region or "IE"):
            profiling_status = "pending"

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
    payload["profiling_status"] = profiling_status
    if ground_truth_nutrition:
        payload["has_ground_truth_nutrition"] = True
        payload["ground_truth_nutrition_source"] = ground_truth_nutrition.get("nutrition_source")
        payload["ground_truth_nutrition"] = ground_truth_nutrition
    else:
        payload["has_ground_truth_nutrition"] = False
    profile_details = _as_list_of_dicts(
        stored_trace.get("nutrition_profiling_details") if isinstance(stored_trace, dict) else None
    )
    profile_debug = _as_dict(
        stored_trace.get("nutrition_profiling_debug") if isinstance(stored_trace, dict) else None
    )
    sustainability_details = _as_list_of_dicts(
        stored_trace.get("sustainability_profiling_details") if isinstance(stored_trace, dict) else None
    )

    if profile_details:
        payload["nutrition_profiling_details"] = profile_details
    if profile_debug:
        payload["nutrition_profiling_debug"] = profile_debug
    if sustainability_details:
        payload["sustainability_profiling_details"] = sustainability_details

    if isinstance(stored_trace, dict):
        payload.update({
            "total_sustainability": stored_trace.get("total_sustainability"),
            "total_sustainability_per_serving": stored_trace.get("total_sustainability_per_serving"),
            "sustainability_per_kg": stored_trace.get("sustainability_per_kg"),
        })

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
    # A pending-profile response must not be cached: the background job
    # invalidates on completion, and a cached "pending" would outlive it.
    if not include_disabled and profiling_status != "pending":
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
        "total_sustainability": profile_result.get("total_sustainability"),
        "total_sustainability_per_serving": profile_result.get("total_sustainability_per_serving"),
        "sustainability_per_kg": profile_result.get("sustainability_per_kg"),
        "sustainability_profiling_details": profile_result.get("sustainability_profiling_details"),
        "trace": profile_result,
        "pipeline_version": profile_pipeline_version,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_recipe_profile_trace(trace_payload)
    cache_delete(recipe_id)
    return True, None


# In-flight guard for background live-profiling jobs: one job per
# (recipe_id, region) at a time, no matter how many GETs race on it.
_LIVE_PROFILE_JOBS: set[tuple[str, str]] = set()
_LIVE_PROFILE_JOBS_LOCK = threading.Lock()


def _schedule_live_profile_job(recipe_id: str, recipe: dict[str, Any], region: str) -> bool:
    """Queue background profiling for a recipe with no stored trace.

    Returns True when a job is already running or was just scheduled, False
    when the recipe has nothing to profile.
    """
    if not (recipe.get("ingredients") or []):
        return False
    key = (str(recipe_id), (region or "IE").strip().upper())
    with _LIVE_PROFILE_JOBS_LOCK:
        if key in _LIVE_PROFILE_JOBS:
            return True
        _LIVE_PROFILE_JOBS.add(key)
    try:
        threading.Thread(
            target=_run_live_profile_job,
            args=(key, dict(recipe)),
            name=f"live-profile-{recipe_id}",
            daemon=True,
        ).start()
    except Exception:
        with _LIVE_PROFILE_JOBS_LOCK:
            _LIVE_PROFILE_JOBS.discard(key)
        raise
    return True


def _run_live_profile_job(key: tuple[str, str], recipe: dict[str, Any]) -> None:
    """Profile a recipe, persist the trace, and drop its cached responses.

    Persisting through _persist_profile_trace_best_effort also gives the
    adaptation service the trace it requires, and its cache_delete makes the
    next GET pick the stored profile up.
    """
    recipe_id, region = key
    try:
        ingredients = recipe.get("ingredients") or []
        ingredient_lines = [
            f"{ing.get('measurement', '')} {ing.get('name', '')}".strip()
            if isinstance(ing, dict) else str(ing)
            for ing in ingredients
        ]
        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
        live_result = Recipe_Profiling_Chain_Structured.invoke({
            "title": recipe.get("title", ""),
            "ingredient_names": ingredient_names,
            "measurements": measurements,
            "serves": float(recipe.get("serves") or 4),
            "total_time": recipe.get("duration"),
            "directions": recipe.get("instructions") or [],
            "region": region,
            "debug": False,
        })
        if isinstance(live_result, dict):
            persisted, warning = _persist_profile_trace_best_effort(
                {"recipe_id": recipe_id}, live_result
            )
            if not persisted:
                logger.warning(
                    "Live profiling for %s (%s) completed but was not persisted: %s",
                    recipe_id, region, warning,
                )
        else:
            logger.warning(
                "Live profiling for %s (%s) returned unexpected payload type %s",
                recipe_id, region, type(live_result).__name__,
            )
    except Exception:
        logger.warning("Live profiling failed for %s (%s)", recipe_id, region, exc_info=True)
    finally:
        with _LIVE_PROFILE_JOBS_LOCK:
            _LIVE_PROFILE_JOBS.discard(key)


def _es_card(card: dict[str, Any]) -> dict[str, Any]:
    """Shape an es_recipe_search card to the search-response card contract."""

    return {
        "recipe_id": card.get("recipe_id"),
        "title": card.get("title"),
        "url": card.get("url"),
        "source": card.get("source"),
        "source_id": card.get("source_id"),
        "image_url": card.get("image_url"),
        "duration": card.get("duration"),
        "serves": card.get("serves"),
        "cost_category": card.get("cost_category"),
        "nutri_score": card.get("nutri_score"),
        "nutri_score_color": card.get("nutri_color"),
        "sust_score": card.get("sust_score"),
        "expert_recipe": card.get("expert_recipe", False),
        "status": card.get("status") or "active",
    }


@router.post(
    "/foodchat_candidates",
    response_model=FoodChatResponse,
    tags=["recipes", "foodchat"],
    summary="Retrieve diverse, customized meal candidates for FoodChat",
)
def get_foodchat_candidates(request: FoodChatRequest) -> FoodChatResponse:
    """Retrieve diverse, filtered recipe candidates grouped by meal slot.

    Designed for multi-day meal plan generation. Each key in ``quotas`` is a
    dish-type tag (e.g. ``"breakfast"``, ``"lunch"``, ``"dinner"``) and its
    value is how many recipes to return for that slot. The response mirrors
    the same keys, each containing a list of recipe items.

    **Filtering (hard constraints)**

    - ``user_profile.allergies`` — excluded via the food taxonomy graph.
      Excluding ``"dairy"`` also excludes recipes whose ingredients are
      taxonomic descendants of dairy (e.g. parmesan, whey).
    - ``user_profile.diet`` — recipe must carry *all* requested diet tags
      (e.g. ``"vegan"``, ``"gluten-free"``). Tags not present in the database
      are silently ignored to avoid empty results from typos.
    - ``constraints.exclude_ingredients`` — hard ingredient exclusion
      (substring + taxonomy ancestor match).
    - ``constraints.exclude_recipe_ids`` — pass previously selected recipe IDs
      to guarantee those are never returned. IDs picked in earlier slots within
      the same call are also automatically excluded from subsequent slots.
    - ``constraints.nutrition_profile`` — per-serving macro range filter
      (min/max for calories, protein, carbs, fat). Applied as a post-filter on
      a 5× candidate pool. Recipes with *no stored nutrition data always pass
      through* — they are never silently dropped.

    **Ranking (soft preferences)**

    - ``constraints.include_ingredients`` — recipes containing these
      ingredients are ranked higher; not a hard filter.
    - ``constraints.favorite_recipe_ids`` — favorited recipes are boosted to
      the top of their meal slot; not a hard filter. Favorites still pass
      through all hard constraints above (a favorite violating an allergy or
      listed in ``exclude_recipe_ids`` is never returned).
    - ``randomize`` (default ``true``) — when ``true`` results are randomly
      ordered, giving different recipes on each call and maximising week-plan
      diversity. Set to ``false`` to rank by ingredient match score instead.

    **Response per recipe**

    Each item contains ``recipe_id``, ``title``, ``ingredients`` (comma-joined
    original strings), ``directions`` (instructions joined as a single string),
    ``dish_type`` (the authoritative server-side tag — no client-side
    classification needed), and ``nutrition`` (``calories``, ``protein_g``,
    ``carbs_g``, ``fat_g`` per serving; ``null`` when no profile is stored).
    """
    try:
        results = fetch_foodchat_candidates(request)
        return FoodChatResponse(results=results)
    except Exception as exc:
        raise InternalError("Failed to fetch foodchat candidates") from exc



def _build_card_nutrition(
    recipe_id: str,
    recipe: dict[str, Any],
    nutrition: dict[str, Any] | None,
    allergens: list[str],
    nutri_score: object,
) -> RecipeCardNutrition:
    """Assemble a slim card with per-serving macros from Neo4j metadata + stored nutrition."""

    kcal = protein = carbs = fat = None
    if nutrition:
        total_nutrients = _as_dict(nutrition.get("total_nutrients"))
        total_nutrients_per_serving = _as_dict(nutrition.get("total_nutrients_per_serving"))
        nutrient_basis = (
            total_nutrients_per_serving
            if isinstance(total_nutrients_per_serving, dict)
            else total_nutrients
        )
        kcal = _extract_nutrient_value(
            nutrient_basis,
            ["Energy", "Energy (kcal)", "Energy, kcal"],
        )
        protein = _extract_nutrient_value(nutrient_basis, ["Protein"])
        carbs = _extract_nutrient_value(
            nutrient_basis,
            ["Carbohydrate", "Carbohydrate, by difference", "Carbohydrate, by diff."],
        )
        fat = _extract_nutrient_value(
            nutrient_basis,
            ["Total lipid (fat)", "Fat", "Total fat"],
        )
        if not isinstance(total_nutrients_per_serving, dict):
            serves = recipe.get("serves")
            kcal = _per_serving(kcal, serves)
            protein = _per_serving(protein, serves)
            carbs = _per_serving(carbs, serves)
            fat = _per_serving(fat, serves)

    label = nutri_score.strip() if isinstance(nutri_score, str) and nutri_score.strip() else None

    return RecipeCardNutrition(
        recipe_id=recipe_id,
        title=recipe.get("title"),
        image_url=recipe.get("image_url"),
        duration=recipe.get("duration"),
        tags=recipe.get("tags") or [],
        dish_types=recipe.get("dish_types") or [],
        allergens=allergens,
        kcal_per_serving=kcal,
        protein_g_per_serving=protein,
        carbs_g_per_serving=carbs,
        fat_g_per_serving=fat,
        nutri_score_label=label,
    )


@router.post(
    "/details",
    response_model=RecipeDetailsBatchResponse,
    tags=["recipes", "foodchat"],
    summary="Batch retrieve slim recipe cards with per-serving macros",
)
def get_recipe_details_batch(request: RecipeDetailsBatchRequest) -> RecipeDetailsBatchResponse:
    """Batch recipe-details lookup for FoodChat plan enrichment.

    Consumed by FoodChat when enriching generated meal plans and by its
    edit-verification predicates (allergen / macro checks on proposed swaps).

    - Accepts 1-30 recipe ids; ``results`` is keyed by the requested id.
      Missing/unknown ids are simply absent from ``results`` — never an error.
    - **Guarantee:** per-serving macros (kcal/protein/carbs/fat) come from the
      nutrition store when a stored profile exists, else they are ``null``.
      When only whole-recipe totals are stored they are divided by ``serves``.
    - ``region`` namespaces the per-recipe response cache; the batch nutrition
      lookup returns the most recently updated stored profile per recipe.
    - Read-only and batch-shaped: one Redis MGET, then for cache misses only a
      single bulk Neo4j metadata query, one batch Postgres nutrition query,
      and batch allergen/score queries.
    """
    variant = _card_nutrition_cache_variant(request.region)
    requested_ids = list(dict.fromkeys(request.recipe_ids))

    results: dict[str, RecipeCardNutrition] = {}
    for rid, data in cache_mget(requested_ids, variant=variant).items():
        try:
            results[rid] = RecipeCardNutrition(**data)
        except Exception:
            cache_delete(rid, variant=variant)

    missing = [rid for rid in requested_ids if rid not in results]
    if not missing:
        return RecipeDetailsBatchResponse(results=results)

    try:
        recipes = fetch_recipe_info_by_ids(missing)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Neo4j", exc) from exc

    if not recipes:
        return RecipeDetailsBatchResponse(results=results)

    # Nutrition/score stores are keyed by canonical recipe_id; a request may
    # have matched r.id instead, so look up both forms in one batch call.
    found_ids = list(recipes.keys())
    resolved_ids = [
        str(recipe.get("recipe_id"))
        for recipe in recipes.values()
        if _as_id(recipe.get("recipe_id"))
    ]
    nutrition_ids = list(dict.fromkeys(found_ids + resolved_ids))

    try:
        nutrition_map = get_recipe_nutrition_batch(nutrition_ids)
    except Exception:  # noqa: BLE001 - nutrition is best-effort; macros stay null
        nutrition_map = {}

    try:
        allergen_map = fetch_recipe_allergens_by_ids(found_ids)
    except Exception:  # noqa: BLE001
        allergen_map = {}

    try:
        score_map = fetch_recipe_scores_by_ids(found_ids)
    except Exception:  # noqa: BLE001
        score_map = {}

    fresh: dict[str, dict[str, Any]] = {}
    for rid in missing:
        recipe = recipes.get(rid)
        if not recipe:
            continue
        resolved_id = _as_id(recipe.get("recipe_id")) or rid
        card = _build_card_nutrition(
            recipe_id=resolved_id,
            recipe=recipe,
            nutrition=nutrition_map.get(resolved_id) or nutrition_map.get(rid),
            allergens=allergen_map.get(rid, []),
            nutri_score=(score_map.get(rid) or {}).get("nutri_score"),
        )
        results[rid] = card
        fresh[rid] = card.model_dump(mode="json")

    if fresh:
        cache_mset(fresh, variant=variant)

    return RecipeDetailsBatchResponse(results=results)


@router.post(
    "/search",
    response_model=None,
    tags=["recipes"],
    summary="Search recipes via the SEARCH_BACKEND engine (Elasticsearch by default; Neo4j legacy)",
)
async def recipe_search(
    payload: RecipeSearchRequest,
) -> dict[str, Any]:
    """Invoke the recipe search LangGraph pipeline and return its output."""

    question = str(payload.question or "").strip()
    exclude_allergens = payload.exclude_allergens if isinstance(payload.exclude_allergens, list) else []
    # Page size for the random-landing and fallback paths (the request model
    # carries no limit field; the primary pipeline uses its own default).
    limit = 10

    # If no free-text question is provided, return a random landing page.
    if not question:
        random_results: list[dict[str, Any]] = []
        try:
            random_results = await run_in_threadpool(_random_myplate_from_elastic, limit=limit)
        except Exception:  # noqa: BLE001
            random_results = []

        return {"results": random_results or []}

    # Lazily initialize Neo/Groq search stack only for non-empty queries.
    recipe_search_app = get_recipe_search_app()

    # Elasticsearch backend: reuse the LLM constraint extractor, then retrieve
    # from the recipes_v2 index instead of composing/executing Cypher.
    if get_settings().search_backend == "es":
        try:
            constraints = recipe_search_app.run_extract_constraints(question)["query_constraints"]
            es_out = search_recipes_es(
                RecipeSearchConstraints(
                    include_ingredients=constraints.get("preferred_ingredients") or [],
                    exclude_ingredients=constraints.get("excluded_ingredients") or [],
                    exclude_allergens=list({*(constraints.get("allergens") or []), *exclude_allergens}),
                    diet_tags=constraints.get("diet") or [],
                    title_keywords=constraints.get("title_keywords") or [],
                    max_duration_minutes=constraints.get("max_duration_minutes"),
                    min_servings=constraints.get("min_servings"),
                    limit=constraints.get("limit") or 10,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise map_dependency_error("Elasticsearch", exc) from exc
        return {"results": [_es_card(card) for card in es_out["results"]]}

    try:
        result = await run_in_threadpool(
            recipe_search_app.invoke, question, exclude_allergens
        )
    except Exception as exc:  # noqa: BLE001 - bubble up as HTTP error
        # Keep the endpoint usable even if the primary graph/LLM path fails.
        try:
            fallback_results = await run_in_threadpool(
                _search_elastic_keyword, question, limit=limit
            )
        except Exception:
            fallback_results = await run_in_threadpool(
                _random_myplate_from_elastic, limit=limit
            )

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
    summary="Deterministic parameter-based recipe search via SEARCH_BACKEND (Elasticsearch by default; Neo4j legacy)",
)
def param_search(payload: RecipeSearchFilters) -> dict[str, Any]:
    """Run deterministic parameter-based recipe search and return results."""

    if get_settings().search_backend == "es":
        try:
            es_out = search_recipes_es(
                RecipeSearchConstraints(
                    include_ingredients=payload.include_ingredients,
                    exclude_ingredients=payload.exclude_ingredients,
                    exclude_allergens=payload.exclude_allergens,
                    diet_tags=payload.diet_tags,
                    sources=payload.sources,
                    dish_types=payload.dish_types,
                    max_duration_minutes=payload.max_duration_minutes,
                    limit=payload.limit,
                    offset=payload.offset,
                    include_facets=payload.include_facets,
                    sort_by=payload.sort_by,
                    include_disabled=payload.include_disabled,
                )
            )
        except ResultWindowExceededError as exc:
            raise InvalidError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise map_dependency_error("Elasticsearch", exc) from exc
        return {
            "results": [_es_card(card) for card in es_out["results"]],
            "total": es_out.get("total", 0),
            "facets": es_out.get("facets", {}),
        }

    try:
        results = search_recipes_by_params(payload)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Neo4j", exc) from exc

    # search_recipes_by_params returns {results, total, facets} since the
    # facets/total rework; tolerate the legacy bare-list shape too.
    extra: dict = {}
    if isinstance(results, dict):
        extra = {k: v for k, v in results.items() if k in ("total", "facets")}
        results = results.get("results", [])

    cards = []
    for row in results:
        if not isinstance(row, dict):
            cards.append(row)
            continue
        nutri_score = row.get("nutri_score")
        cards.append({
            "recipe_id": row.get("recipe_id"),
            "title": row.get("title"),
            "url": row.get("url"),
            "source": row.get("source"),
            "source_id": row.get("source_id"),
            "image_url": row.get("image_url"),
            "duration": row.get("duration"),
            "serves": row.get("serves"),
            "cost_category": row.get("cost_category"),
            "nutri_score": nutri_score,
            "nutri_score_color": _nutri_color_from_score(nutri_score),
            "sust_score": row.get("sust_score"),
            "expert_recipe": row.get("expert_recipe", False),
            "status": row.get("status") or "active",
        })
    return {"results": cards, **extra}


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
    trusted_serves = payload.serves

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
        serves = trusted_serves or parsed.get("serves") or 0
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
            {
                "recipe_text": raw_recipe,
                "debug": False,
                "region": region,
                "trusted_serves": trusted_serves,
            }
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


def _project_to_recipes_v2(recipe_id: str) -> None:
    """Refresh this recipe's full recipes_v2 doc from Neo4j+Postgres.

    Best-effort (logged inside): the owners hold the truth and the offline
    rebuild converges the index if this write is missed.
    """
    from recipe_wrangler.tools.es_recipe_search import ES_INDEX

    settings = get_settings()
    project_recipe_to_es_v2(recipe_id, es_url=settings.elastic_url, index=ES_INDEX)


def _index_recipe_to_elastic(
    recipe_id: str,
    title: str,
    ingredient_names: list[str],
    tags: list[str],
    source: str,
    source_id: str | None,
) -> None:
    """Index a single recipe document into Elasticsearch (best-effort)."""
    settings = get_settings()
    url = f"{settings.elastic_url}/{settings.elastic_index}/_doc/{recipe_id}"
    doc = {
        "id": recipe_id,
        "title": title,
        "source": source,
        "source_id": resolve_collection_source_id(source, source_id),
        "ingredients": ingredient_names,
        "tags": tags,
    }
    get_http_session().put(url, json=doc, timeout=settings.elastic_timeout)


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
            profile_result = await _invoke_profile_with_timeout({
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
        "total_sustainability": profile_result.get("total_sustainability") if profile_result else None,
        "total_sustainability_per_serving": profile_result.get("total_sustainability_per_serving") if profile_result else None,
        "sustainability_per_kg": profile_result.get("sustainability_per_kg") if profile_result else None,
        "sustainability_profiling_details": profile_result.get("sustainability_profiling_details") if profile_result else None,
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
        _index_recipe_to_elastic(
            recipe_id,
            payload.title,
            ingredient_names,
            merged_tags,
            "user",
            payload.source_id,
        )
    except Exception:
        pass  # non-fatal

    # Project the full doc into the primary search index — without this the
    # recipe exists in Neo4j/Postgres but never appears in recipes_v2 until an
    # offline rebuild. Runs after the Postgres trace write so nutri scores land.
    _project_to_recipes_v2(recipe_id)

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

    profile_error: str | None = None
    try:
        profile_result = await _invoke_profile_with_timeout({
            "title": recipe.get("title") or "",
            "ingredient_names": modified_ingredient_names,
            "measurements": modified_measurements,
            "serves": serves,
            "total_time": float(total_time) if total_time is not None else None,
            "directions": recipe.get("instructions") or [],
            "region": region,
            "debug": False,
        })
        if not isinstance(profile_result, dict):
            raise InternalError(
                detail="Profiling pipeline returned unexpected payload",
                extra={"title": "ProfilingPipelineError"},
            )

        # Strip top-level None values (unset pipeline state)
        profile_result = {k: v for k, v in profile_result.items() if v is not None}
    except Exception as exc:
        profile_error = str(exc)
        profile_result = {
            "status": "profiling_unavailable",
            "region": region,
            "title": recipe.get("title") or "",
            "serves": serves,
            "modified_ingredients": modified_ingredient_names,
            "measurements": modified_measurements,
            "error": profile_error,
        }

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
    resolved_cache_id: str | None = None
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

    # --- Elasticsearch legacy index (image_url only — its docs carry no other
    # patchable field the runtime serves) ---
    if payload.image_url is not None:
        try:
            settings = get_settings()
            url = f"{settings.elastic_url}/{settings.elastic_index}/_update/{recipe_id}"
            get_http_session().post(
                url,
                json={"doc": {"image_url": payload.image_url}},
                timeout=settings.elastic_timeout,
            )
        except Exception:
            pass  # non-fatal

    # --- recipes_v2: full-doc reprojection so title/tags/allergens/duration/
    # expert_recipe edits reach search instead of going stale until a rebuild.
    _project_to_recipes_v2(resolved_cache_id or recipe_id)

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


# ---------------------------------------------------------------------------
# Recipe soft-delete (disable/enable) endpoints
# ---------------------------------------------------------------------------

_STATUS_RESPONSE_ID_CAP = 1000


def _es_status_indices() -> tuple[str, list[str]]:
    """Both indices that serve recipes: recipes_v2 (search) + the legacy
    autocomplete/fallback index."""
    settings = get_settings()
    from recipe_wrangler.tools.es_recipe_search import ES_INDEX
    indices = list(dict.fromkeys([ES_INDEX, settings.elastic_index]))
    return settings.elastic_url, indices


def _apply_recipe_status(
    recipe_ids: list[str],
    status: str,
    reason: str | None,
) -> RecipeStatusResponse:
    """Shared write path: Neo4j status flip -> ES dual-index sync -> cache purge."""
    try:
        updated_ids = set_recipe_status(recipe_ids, status, reason)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Neo4j", exc) from exc

    es_stats: dict[str, dict[str, int]] = {}
    if updated_ids:
        es_url, indices = _es_status_indices()
        # Best-effort: a failed ES sync is reported in the response, never fatal —
        # Neo4j is the source of truth and a re-run converges ES.
        es_stats = sync_recipe_status_to_es(updated_ids, status, es_url=es_url, indices=indices)

        # Canonical IDs and requested aliases (r.id lookups) may each have
        # their own cache keys — purge both in one batched pass.
        cache_delete_many({*updated_ids, *(str(rid) for rid in recipe_ids)})

    return RecipeStatusResponse(
        status=status,  # type: ignore[arg-type]
        requested=len(recipe_ids),
        updated=len(updated_ids),
        recipe_ids=updated_ids[:_STATUS_RESPONSE_ID_CAP],
        es_sync=es_stats,
        message=f"{len(updated_ids)} recipe(s) set to '{status}'",
    )


@router.post(
    "/disable",
    response_model=RecipeStatusResponse,
    tags=["recipes"],
    summary="Bulk disable (soft-delete) recipes by explicit IDs",
)
def recipes_bulk_disable(payload: RecipeBulkStatusRequest) -> RecipeStatusResponse:
    """Disable every listed recipe so it is never served to any consumer.

    Reversible via the enable endpoints; recipe data is retained everywhere.
    """
    response = _apply_recipe_status(payload.recipe_ids, STATUS_DISABLED, payload.reason)
    if response.updated == 0:
        raise NotFoundError(detail="No recipes matched the provided IDs")
    return response


@router.post(
    "/enable",
    response_model=RecipeStatusResponse,
    tags=["recipes"],
    summary="Bulk re-enable previously disabled recipes by explicit IDs",
)
def recipes_bulk_enable(payload: RecipeBulkStatusRequest) -> RecipeStatusResponse:
    response = _apply_recipe_status(payload.recipe_ids, STATUS_ACTIVE, None)
    if response.updated == 0:
        raise NotFoundError(detail="No recipes matched the provided IDs")
    return response


def _claim_status_job(status: str, requested: int) -> None:
    """Mark a by-query job in flight; raise 409 if one is already running."""
    running = status_job_guard.try_claim(status, requested)
    if running is not None:
        raise ConflictError(
            f"A bulk status job is already running "
            f"(status='{running['status']}', {running['requested']} recipes, "
            f"started {running['running_for_s']:.0f}s ago). "
            "Retry after it completes."
        )


def _run_status_job(recipe_ids: list[str], status: str, reason: str | None) -> None:
    """Background body of by-query status flips — the request has already
    returned 202, so failures can only be surfaced in the logs."""
    started = time.monotonic()
    try:
        response = _apply_recipe_status(recipe_ids, status, reason)
        logger.info(
            "Background status job done status=%s requested=%d updated=%d in %.1fs",
            status, len(recipe_ids), response.updated, time.monotonic() - started,
        )
    except Exception:
        logger.exception(
            "Background status job failed status=%s requested=%d", status, len(recipe_ids),
        )
    finally:
        status_job_guard.release()


@router.post(
    "/disable-by-query",
    response_model=RecipeStatusResponse,
    status_code=202,
    tags=["recipes"],
    summary="Bulk disable every recipe matching param_search filters (async)",
)
def recipes_disable_by_query(
    payload: RecipeDisableByQueryRequest,
    background_tasks: BackgroundTasks,
) -> RecipeStatusResponse:
    """Resolve the matching ID set via the param_search WHERE clause, then
    disable in the background. Returns 202 immediately with the matched count
    (`requested`); `updated` is always 0 here — poll param_search counts to
    watch progress. Large sets would otherwise outlive the gateway timeout.
    """
    from recipe_wrangler.tools.param_search import _build_where_clause, _has_no_constraints

    filters = RecipeSearchFilters(**payload.model_dump(exclude={"reason", "allow_unfiltered"}))
    if _has_no_constraints(filters) and not payload.allow_unfiltered:
        raise InvalidError(
            "Refusing an unconstrained disable-by-query (it would disable every "
            "recipe). Pass allow_unfiltered=true if that is really intended."
        )

    where_clause, params = _build_where_clause(filters)
    try:
        matched_ids = resolve_recipe_ids_by_query(where_clause, params)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Neo4j", exc) from exc

    if not matched_ids:
        return RecipeStatusResponse(
            status=STATUS_DISABLED,
            requested=0,
            updated=0,
            message="No recipes matched the query",
        )

    _claim_status_job(STATUS_DISABLED, len(matched_ids))
    background_tasks.add_task(_run_status_job, matched_ids, STATUS_DISABLED, payload.reason)
    return RecipeStatusResponse(
        status=STATUS_DISABLED,
        requested=len(matched_ids),
        updated=0,
        message=f"Disabling {len(matched_ids)} recipe(s) in the background",
    )


@router.post(
    "/{recipe_id}/disable",
    response_model=RecipeStatusResponse,
    tags=["recipes"],
    summary="Disable (soft-delete) a single recipe",
)
def recipe_disable(recipe_id: str, payload: RecipeDisableRequest | None = None) -> RecipeStatusResponse:
    response = _apply_recipe_status([recipe_id], STATUS_DISABLED, payload.reason if payload else None)
    if response.updated == 0:
        raise NotFoundError(detail=f"Recipe {recipe_id} not found")
    return response


@router.post(
    "/{recipe_id}/enable",
    response_model=RecipeStatusResponse,
    tags=["recipes"],
    summary="Re-enable a previously disabled recipe",
)
def recipe_enable(recipe_id: str) -> RecipeStatusResponse:
    response = _apply_recipe_status([recipe_id], STATUS_ACTIVE, None)
    if response.updated == 0:
        raise NotFoundError(detail=f"Recipe {recipe_id} not found")
    return response
