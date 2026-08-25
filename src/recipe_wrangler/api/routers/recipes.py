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

import re
import requests
from fastapi import APIRouter, BackgroundTasks, Depends, Query
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
from recipe_wrangler.utils.http_pool import get_http_session, post_query_with_retry

from recipe_wrangler.api.identity import Caller, get_caller, redact
from recipe_wrangler.catalog.sources import (
    canonical_course_type,
    ground_truth_nutrition_sources,
)
from recipe_wrangler.catalog.foodchat import fetch_candidates_es
from recipe_wrangler.catalog.entities import recipe_entity
from recipe_wrangler.catalog.writer import commit as commit_recipe
from recipe_wrangler.tools.es_recipe_search import (
    ES_INDEX,
    RecipeSearchConstraints,
    ResultWindowExceededError,
    normalize_recipe_title,
    search_recipes_es,
)
from recipe_wrangler.tools.recipe_search_constraints import (
    resolve_ingredient_allergen_conflicts,
)
from recipe_wrangler.utils.recipe_cache import (
    cache_delete,
    cache_delete_many,
    cache_get,
    cache_mget,
    cache_mset,
    cache_set,
)
from recipe_wrangler.utils.recipe_status import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    es_not_disabled_clause,
    status_job_guard,
    sync_recipe_status_to_es,
)
from recipe_wrangler.utils.neo4j_utils import run_query as _run_query
from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergen_evidence_from_names,
    detect_allergens_from_names,
    fetch_recipe_dish_types_by_ids,
    find_ingredient_substitutes,
    infer_diet_tags,
    replace_recipe_nutrition_claims,
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
    get_recipe_profile_traces,
    save_recipe_profile_trace,
)
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
from recipe_wrangler.utils.nutrition_claims import (
    compute_nutrition_claim_tags,
    infer_physical_form,
)
from recipe_wrangler.utils.fruit_vegetable_content import fruits_veg_legumes_percent

logger = logging.getLogger(__name__)

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

from ..dependencies import get_recipe_constraint_extractor
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
    RecipeUrlRequest,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])

_RECIPE_BASE_CACHE_VARIANT = "base"


def _analysis_allergen_fields(ingredient_names: list[str]) -> dict[str, Any]:
    """Return paired, explainable keyword evidence for an unpersisted recipe."""
    evidence = detect_allergen_evidence_from_names(ingredient_names)
    return {
        "allergens": sorted({row["allergen"] for row in evidence}),
        "allergen_evidence": evidence,
    }


def _profile_meta() -> str:
    settings = get_settings()
    return settings.profile_pipeline_version


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _hit_recipe_id(source: dict[str, Any], hit: dict[str, Any]) -> str | None:
    """Resolve the public recipe id, never the catalog row UUID."""
    recipe_id = _as_id(source.get("recipe_id"))
    if recipe_id:
        return recipe_id
    document_id = _as_id(hit.get("_id"))
    if document_id and document_id.startswith("urn:recipe:"):
        return document_id.split(":", 2)[2]
    return document_id


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


def _catalog_recipe_payload(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert a v4 catalog document to the established v1 detail shape."""
    recipe = dict(doc)
    recipe_id = _as_id(recipe.get("recipe_id"))
    if recipe_id:
        recipe["recipe_id"] = recipe_id

    instructions = recipe.get("instructions")
    if isinstance(instructions, str):
        recipe["instructions"] = [
            line.strip() for line in instructions.splitlines() if line.strip()
        ]
    elif isinstance(instructions, (list, tuple)):
        recipe["instructions"] = [
            str(line).strip() for line in instructions if str(line).strip()
        ]
    else:
        recipe["instructions"] = []

    ingredients: list[dict[str, Any]] = []
    for position, item in enumerate(recipe.get("ingredients") or []):
        if isinstance(item, dict):
            entry = dict(item)
            entry.setdefault("position", position)
        else:
            name = str(item or "").strip()
            if not name:
                continue
            entry = {"name": name, "position": position}
        if str(entry.get("name") or "").strip():
            ingredients.append(entry)
    recipe["ingredients"] = ingredients
    recipe["tags"] = list(recipe.get("tags") or [])
    recipe["allergens"] = list(recipe.get("allergens") or [])
    # The old FoodChat card field was named dish_types. Keep its response
    # contract while reading the canonical v4 course_types field.
    recipe["dish_types"] = list(recipe.get("course_types") or [])
    recipe.setdefault("status", "active")
    return recipe


def _catalog_recipe_by_id(
    recipe_id: str, *, include_disabled: bool = False
) -> dict[str, Any] | None:
    doc = recipe_entity().get(recipe_id)
    if not doc:
        return None
    if not include_disabled and str(doc.get("status") or "active").lower() == STATUS_DISABLED:
        return None
    return _catalog_recipe_payload(doc)


def _catalog_recipes_by_ids(recipe_ids: list[str]) -> dict[str, dict[str, Any]]:
    docs = recipe_entity().get_many(recipe_ids)
    return {
        requested_id: _catalog_recipe_payload(doc)
        for requested_id, doc in docs.items()
        if str(doc.get("status") or "active").lower() != STATUS_DISABLED
    }


def _catalog_recipe_id_by_title(title: str) -> str | None:
    normalized = normalize_recipe_title(title)
    if not normalized:
        return None
    result = recipe_entity().search(
        filters=[{"term": {"title_normalized": normalized}}],
        limit=1,
        source_fields=["recipe_id"],
    )
    rows = result.get("results") or []
    return _as_id(rows[0].get("recipe_id")) if rows else None


def _nutrition_source_from_region(region: str | None) -> str | None:
    if region is None:
        return None
    region_norm = str(region).strip().upper()
    if not region_norm:
        return None
    mapping = {
        "IE": "irish",
        "HU": "hungarian",
        "EU": "eu",
        "SI": "slovenian",
    }
    return mapping.get(region_norm)


def _recipe_response_cache_variant(region: str | None, slim: bool) -> str:
    region_key = str(region or "default").strip().upper() or "DEFAULT"
    region_key = "".join(ch if ch.isalnum() else "_" for ch in region_key)
    # v2: detail responses carry `allergens`. Bumping the variant retires
    # pre-allergen cache entries, which would otherwise keep serving payloads
    # with no allergens (indistinguishable from "this recipe has none").
    return f"detail:v2:region:{region_key}:slim:{int(slim)}"


# Bumped whenever the card gains a field. An entry cached before the field
# existed still PARSES — every field has a default — so it comes back with the
# new one empty, and a consumer that checks `diet_tags` would read a cached
# vegetarian recipe as not vegetarian. Silent, wrong, and safety-shaped, so the
# old entries have to be unreachable rather than merely stale.
#
#   v2: diet_tags added
_CARD_NUTRITION_CACHE_VERSION = "v2"


def _card_nutrition_cache_variant(region: str | None) -> str:
    region_key = str(region or "default").strip().upper() or "DEFAULT"
    region_key = "".join(ch if ch.isalnum() else "_" for ch in region_key)
    return f"card_nutrition:{_CARD_NUTRITION_CACHE_VERSION}:region:{region_key}"


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
        "_source": ["recipe_id", "title", "image_url"],
        "query": {
            "function_score": {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"source": "MyPlate"}},
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
        rid = _hit_recipe_id(source, hit)
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
        "_source": ["recipe_id", "title", "image_url", "source"],
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
        rid = _hit_recipe_id(source, hit)
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
    score_ingredients: list[dict[str, Any]] = []
    for row in profile_details:
        weight = _coerce_float(row.get("weight_g"))
        if weight is None or weight <= 0:
            continue
        total_weight_g += weight
        ingredient_name = row.get("ingredient") or ""
        ingredient = {"name": ingredient_name, "weight_grams": weight}
        for key in ("food_groups", "ingredient_class_ancestors"):
            if row.get(key):
                ingredient[key] = row[key]
        score_ingredients.append(ingredient)

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
            fruits_veg_legumes_percent(score_ingredients) if score_ingredients else 0.0
        ),
    }

    breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
    breakdown["inputs"] = {
        "total_weight_g": total_weight_g,
        "ingredients_evaluated_for_fvln_count": len(score_ingredients),
    }
    return breakdown


def _source_ground_truth_nutrition_source(recipe_source: object) -> str | None:
    sources = ground_truth_nutrition_sources(recipe_source)
    return sources[0] if sources else None


def _source_ground_truth_nutrition_sources(recipe_source: object) -> list[str]:
    return list(ground_truth_nutrition_sources(recipe_source))


def _is_source_ground_truth_trace(trace: dict[str, Any] | None) -> bool:
    if not isinstance(trace, dict) or trace.get("_computed_on_the_fly"):
        return False
    nutrition_source = str(trace.get("nutrition_source") or "").strip().lower()
    pipeline_version = str(trace.get("pipeline_version") or "").strip().lower()
    return (
        nutrition_source in {
            "safefood_rcsi",
            "safefood_web",
            "safefood",
            "healthyfoods",
            "healthyfoods_original",
            "myplate",
            "planeat",
            "slovenian_original",
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


_NUTRI_GRADE_MEANINGS = {
    "A": "most favourable nutrient balance",
    "B": "favourable nutrient balance",
    "C": "middle nutrient balance",
    "D": "less favourable nutrient balance",
    "E": "least favourable nutrient balance",
}


def _nutri_grade(value: object) -> str | None:
    text = str(value or "").strip().upper().replace("NUTRISCORE_", "")
    return text if text in _NUTRI_GRADE_MEANINGS else None


def _nutri_score_explanation(
    label: object,
    breakdown: dict[str, Any] | None,
    recipe_id: str,
) -> dict[str, Any] | None:
    grade = _nutri_grade(label)
    if not grade:
        return None
    negative = _as_dict((breakdown or {}).get("negative_points")) or {}
    positive = _as_dict((breakdown or {}).get("positive_points")) or {}
    negative_items = _as_dict(negative.get("items")) or {}
    positive_items = _as_dict(positive.get("items")) or {}

    def ranked(items: dict[str, Any], *, positive_side: bool) -> list[dict[str, Any]]:
        drivers: list[dict[str, Any]] = []
        for name, raw in items.items():
            item = _as_dict(raw) or {}
            points = _coerce_float(item.get("points")) or 0.0
            if points <= 0:
                continue
            drivers.append(
                {
                    "factor": name,
                    "points": points,
                    "value_per_100g": item.get("value_per_100g"),
                    "unit": item.get("unit"),
                    "effect": "improves_score" if positive_side else "worsens_score",
                    **({"applied": item.get("applied", True)} if positive_side else {}),
                }
            )
        return sorted(drivers, key=lambda item: item["points"], reverse=True)[:3]

    return {
        "grade": grade,
        "meaning": _NUTRI_GRADE_MEANINGS[grade],
        "basis": "Calculated per 100 g from the selected regional ingredient composition data.",
        "main_negative_drivers": ranked(negative_items, positive_side=False),
        "main_positive_drivers": ranked(positive_items, positive_side=True),
        "guidance": (
            "Nutri-Score compares nutrient balance; it is not a judgement of a person, "
            "a single portion, or whether the recipe can never be eaten."
        ),
        "improve_endpoint": f"/api/v1/recipes/{recipe_id}/adapt/suggestions",
    }


def _extract_profiling_quality(stored_trace: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(stored_trace, dict):
        return {}
    quality = _as_dict(stored_trace.get("profiling_quality"))
    if quality:
        return quality
    debug = _as_dict(stored_trace.get("nutrition_profiling_debug")) or {}
    quality = _as_dict(debug.get("profiling_quality"))
    if quality:
        return quality
    profiling = _as_dict(debug.get("profiling")) or {}
    return _as_dict(profiling.get("quality")) or {}


def _calculation_disclaimer(
    quality: dict[str, Any],
    profile_details: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    if not quality:
        reasons.append("quality_metadata_unavailable")
    if quality.get("serves_source") == "estimated":
        reasons.append("servings_estimated")
    if quality.get("weights_capped") is True:
        reasons.append("ingredient_weights_sanity_adjusted")
    if quality.get("nutrition_low_coverage") is True:
        reasons.append("low_nutrition_coverage")
    if quality.get("sustainability_low_coverage") is True:
        reasons.append("low_sustainability_coverage")
    weak = sum(
        1
        for item in profile_details
        if str(item.get("match_confidence") or item.get("confidence") or "").lower()
        in {"weak", "low"}
    )
    if weak:
        reasons.append("weak_ingredient_matches")
    required = bool(reasons)
    return {
        "required": required,
        "reasons": reasons,
        "message": (
            "Nutrition and sustainability values are estimates; use them with caution "
            "because one or more inputs have limited confidence."
            if required
            else "Nutrition and sustainability values are calculated estimates from ingredient matches."
        ),
    }


def _sustainability_explanation(
    total_per_serving: object,
    details: list[dict[str, Any]],
    quality: dict[str, Any],
) -> dict[str, Any] | None:
    total = _coerce_float(total_per_serving)
    if total is None and not details:
        return None
    contributors: list[dict[str, Any]] = []
    for item in details:
        contribution = _coerce_float(item.get("contribution"))
        if contribution is None:
            continue
        contributors.append(
            {
                "ingredient": item.get("name") or item.get("ingredient"),
                "matched_ingredient": item.get("matched_sustainability_ingredient"),
                "kg_co2e": contribution,
            }
        )
    contributors.sort(key=lambda item: item["kg_co2e"], reverse=True)
    return {
        "kg_co2e_per_serving": total,
        "method": "Ingredient weights multiplied by Sustainable FooDB emission factors.",
        "top_contributors": contributors[:3],
        "coverage": quality.get("sustainability_coverage"),
        "guidance": (
            "This is an ingredient-production estimate, not a full life-cycle assessment; "
            "transport, cooking energy, packaging, and waste may be absent."
        ),
    }


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
        # Over-fetch: some candidates may be dropped by the recipes_v2
        # disabled-status cross-check below.
        "size": min(limit * 2, 40),
        "_source": ["recipe_id", "title"],
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
        response = post_query_with_retry(
            url,
            search_payload,
            timeout=settings.elastic_timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise map_dependency_error("Elasticsearch", exc) from exc

    hits = payload.get("hits", {}).get("hits", [])

    # Cross-check the shortlist against recipes_v2 (one _mget, <=40 ids) and
    # DROP anything the primary index doesn't vouch for:
    #  - disabled recipes whose bulk status flip never reached a legacy index, and
    #  - corrupt legacy docs whose stored id is a title or dead short id
    #    ("Leftover Turkey Casserole", "15cdb65ed2") — suggesting those sent
    #    every consumer (UI clicks, FoodChat seed anchoring) into 404s.
    candidate_ids = [
        rid for rid in (
            _as_id((hit.get("_source") or {}).get("id")) or _as_id(hit.get("_id"))
            for hit in hits
        ) if rid
    ]
    excluded_ids: set[str] = set()
    if candidate_ids:
        try:
            # A search on `recipe_id`, not an _mget by _id. The document key
            # differs per index generation — recipes_v2 used the bare recipe id
            # as _id, the catalog index uses `urn:recipe:<id>` — so an _mget by
            # raw id silently finds nothing after the read flip and marks every
            # suggestion as excluded. Querying the field works under either.
            lookup = get_http_session().post(
                f"{settings.elastic_url}/{settings.elastic_index}/_search",
                json={
                    "size": len(candidate_ids),
                    "_source": ["recipe_id", "id", "status"],
                    "query": {
                        "bool": {
                            "should": [
                                {"terms": {"recipe_id": candidate_ids}},
                                {"terms": {"id": candidate_ids}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                },
                timeout=settings.elastic_timeout,
            )
            lookup.raise_for_status()
            found: dict[str, str] = {}
            for hit in lookup.json().get("hits", {}).get("hits") or []:
                src = hit.get("_source") or {}
                status_value = str(src.get("status") or "").lower()
                for key in (src.get("recipe_id"), src.get("id")):
                    if key:
                        found[str(key)] = status_value
            for rid in candidate_ids:
                if rid not in found or found[rid] == STATUS_DISABLED:
                    excluded_ids.add(rid)
        except requests.RequestException:
            # Best-effort: a failed cross-check must never break autocomplete.
            excluded_ids = set()

    suggestions: dict[str, str] = {}
    seen: set[str] = set()
    for hit in hits:
        if len(suggestions) >= limit:
            break
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
        rid = _hit_recipe_id(source, hit)
        if not rid or rid in excluded_ids:
            continue
        seen.add(key)
        suggestions[rid] = normalized

    return {"suggestions": suggestions}


@router.get(
    "/count",
    response_model=None,
    tags=["recipes"],
    summary="Return the total number of active recipes in the catalog",
)
def get_recipe_count() -> dict[str, int]:
    try:
        total = recipe_entity().count(recipe_entity().es.active_query())
    except Exception as exc:
        raise map_dependency_error("Elasticsearch", exc) from exc
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
        description="Optional nutrition region selector: IE, HU, EU, or SI.",
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
            recipe = _catalog_recipe_by_id(recipe_id, include_disabled=include_disabled)
        except Exception as exc:  # noqa: BLE001
            raise map_dependency_error("Elasticsearch", exc) from exc

        if not recipe:
            raise NotFoundError("Recipe not found")

        if not include_disabled:
            cache_set(recipe_id, recipe, variant=_RECIPE_BASE_CACHE_VARIANT)

    # Nutrition/profile stores are keyed by canonical recipe_id.
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

    # One query fetches every region's trace row (sans the archival trace
    # column); the preferred-region / any-region / ground-truth selections all
    # happen in Python. This used to be up to five sequential Postgres reads,
    # each dragging tens of KB of unused trace JSON.
    trace_rows: list[dict[str, Any]] = []
    try:
        trace_rows = get_recipe_profile_traces(resolved_recipe_id)
    except Exception:
        trace_rows = []
    rows_by_source: dict[str, dict[str, Any]] = {}
    for trace_row in trace_rows:
        source_key = str(trace_row.get("nutrition_source") or "").strip().lower()
        rows_by_source.setdefault(source_key, trace_row)

    preferred_trace = None
    if preferred_nutrition_source:
        preferred_trace = rows_by_source.get(
            str(preferred_nutrition_source).strip().lower()
        )
    stored_trace = preferred_trace or (trace_rows[0] if trace_rows else None)

    # Nutrition is derived from stored_trace further below (same table, same
    # row selection the dedicated nutrition query used to make).
    nutrition = None

    source_ground_truth_trace = None
    ground_truth_sources = _source_ground_truth_nutrition_sources(recipe.get("source"))
    for ground_truth_source in ground_truth_sources:
        source_ground_truth_trace = rows_by_source.get(
            str(ground_truth_source or "").strip().lower()
        )
        if source_ground_truth_trace:
            break
    if source_ground_truth_trace is None and _is_source_ground_truth_trace(stored_trace):
        source_ground_truth_trace = stored_trace

    ground_truth_nutrition = (
        _ground_truth_nutrition_payload(source_ground_truth_trace)
        or _healthyfoods_ground_truth_nutrition(recipe)
    )

    # On-the-fly profiling for the region the caller selected. An existing
    # profile for another region may be returned while this completes, but it
    # must not suppress generation of the requested one. The profiling chain is
    # far too slow to block a GET, so it runs in a background thread.
    profiling_status = None
    if preferred_nutrition_source and preferred_trace is None:
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
    profiling_quality = _extract_profiling_quality(stored_trace)
    payload["profiling_quality"] = profiling_quality
    payload["calculation_disclaimer"] = _calculation_disclaimer(
        profiling_quality, profile_details
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
        payload["sustainability_explanation"] = _sustainability_explanation(
            stored_trace.get("total_sustainability_per_serving"),
            sustainability_details,
            profiling_quality,
        )

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

    # The recipe's original Nutri-Score stays authoritative for display: the
    # live profiling pipeline re-matches free-text ingredients and can drift
    # toward better grades on messy ingredient lists. Its recomputed score
    # only fills the gap when the recipe never had one.
    original_nutri_score = str(recipe.get("nutri_score") or "").strip()
    if original_nutri_score:
        payload["nutri_score_label"] = original_nutri_score
        payload["nutri_score_color"] = _nutri_color_from_score(original_nutri_score)

    payload["nutri_score_explanation"] = _nutri_score_explanation(
        payload.get("nutri_score_label"),
        _as_dict(payload.get("nutri_score_breakdown")),
        resolved_recipe_id,
    )

    # Allergen declarations and their FATO/FoodOn evidence are projected into
    # the v4 catalog. Detail reads must not round-trip to Neo4j to rediscover
    # information already owned by the search document.
    payload["allergens"] = sorted(payload.get("allergens") or [])

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
        return _catalog_recipe_id_by_title(title)
    except Exception:
        return None


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
        "profiling_quality": profile_result.get("profiling_quality"),
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


# In-flight guard for background live-profiling jobs: one job per recipe and
# selected region, no matter how many identical GETs race on it.
_LIVE_PROFILE_JOBS: set[str] = set()
_LIVE_PROFILE_JOBS_LOCK = threading.Lock()


def _schedule_live_profile_job(recipe_id: str, recipe: dict[str, Any], region: str) -> bool:
    """Queue background profiling for a recipe with no stored trace.

    Returns True when a job is already running or was just scheduled, False
    when the recipe has nothing to profile.
    """
    if not (recipe.get("ingredients") or []):
        return False
    selected_region = (region or "IE").strip().upper()
    key = f"{recipe_id}:{selected_region}"
    with _LIVE_PROFILE_JOBS_LOCK:
        if key in _LIVE_PROFILE_JOBS:
            return True
        _LIVE_PROFILE_JOBS.add(key)
    try:
        threading.Thread(
            target=_run_live_profile_job,
            args=(str(recipe_id), dict(recipe), selected_region, key),
            name=f"live-profile-{recipe_id}-{selected_region.lower()}",
            daemon=True,
        ).start()
    except Exception:
        with _LIVE_PROFILE_JOBS_LOCK:
            _LIVE_PROFILE_JOBS.discard(key)
        raise
    return True


def _run_live_profile_job(
    recipe_id: str,
    recipe: dict[str, Any],
    region: str,
    job_key: str,
) -> None:
    """Profile only the region selected by the caller and reproject the recipe."""
    try:
        ingredients = recipe.get("ingredients") or []
        ingredient_lines = list(
            dict.fromkeys(recipe.get("original_ingredients") or [])
        )
        if not ingredient_lines:
            ingredient_lines = [
                f"{ing.get('measurement', '')} {ing.get('name', '')}".strip()
                if isinstance(ing, dict) else str(ing)
                for ing in ingredients
            ]
        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
        started = time.perf_counter()
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
        if not isinstance(live_result, dict):
            logger.warning(
                "Live profiling for %s (%s) returned unexpected payload type %s",
                recipe_id, region, type(live_result).__name__,
            )
            return

        persisted, warning = _persist_profile_trace_best_effort(
            {"recipe_id": recipe_id}, live_result
        )
        if not persisted:
            logger.warning(
                "Live profiling for %s (%s) completed but was not persisted: %s",
                recipe_id, region, warning,
            )
            return

        # The profile owner is Postgres, but consumers query Elasticsearch.
        # Reproject after persistence so the selected region becomes visible in
        # profiles[], regions_available and the flat score fields immediately.
        from recipe_wrangler.catalog.projection import project

        project(recipe_id)
        logger.info(
            "Live profiling %s (%s) persisted and projected in %.1fs",
            recipe_id, region, time.perf_counter() - started,
        )
    except Exception:
        logger.warning(
            "Live profiling failed for %s (%s)", recipe_id, region, exc_info=True
        )
    finally:
        with _LIVE_PROFILE_JOBS_LOCK:
            _LIVE_PROFILE_JOBS.discard(job_key)


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
        "allergens": card.get("allergens") or [],
        "allergen_evidence": card.get("allergen_evidence") or [],
        "suitable_for": card.get("suitable_for") or [],
        "consumer_suitability": card.get("consumer_suitability") or [],
        # Already canonicalized by es_recipe_search, so the client sees one
        # spelling regardless of which builder wrote the document.
        "course_types": card.get("course_types") or [],
        # Annotation facets, so a card can display the cuisine or mood that
        # brought it back. Note this dict is an allow-list, not a passthrough:
        # `creator` is absent from it deliberately, and anything added here is
        # visible to every caller including anonymous ones.
        "cuisines": card.get("cuisines") or [],
        "moods": card.get("moods") or [],
        "flavor_profiles": card.get("flavor_profiles") or [],
        "food_groups": card.get("food_groups") or [],
        "convenience": card.get("convenience") or [],
        "nutrition_claims": card.get("nutrition_claims") or [],
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

    - ``user_profile.allergies`` — excluded via projected allergen evidence.
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
        return FoodChatResponse(results=fetch_candidates_es(request))
    except Exception as exc:
        raise map_dependency_error("Elasticsearch", exc) from exc



def _build_card_nutrition(
    recipe_id: str,
    recipe: dict[str, Any],
    nutrition: dict[str, Any] | None,
    allergens: list[str],
    nutri_score: object,
) -> RecipeCardNutrition:
    """Assemble a slim card with catalog metadata plus stored nutrition."""

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
        diet_tags=recipe.get("diet_tags") or [],
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
      single Elasticsearch mget and one batch Postgres nutrition query.
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
        recipes = _catalog_recipes_by_ids(missing)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc

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
            allergens=list(recipe.get("allergens") or []),
            nutri_score=recipe.get("default_nutri_score"),
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
    summary="Search recipes in Elasticsearch from a natural-language question",
)
async def recipe_search(
    payload: RecipeSearchRequest,
) -> dict[str, Any]:
    """Interpret a recipe question and retrieve matching recipes."""

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

    extractor = await run_in_threadpool(get_recipe_constraint_extractor)
    extract_started = time.perf_counter()
    try:
        constraints = (
            await run_in_threadpool(extractor.run_extract_constraints, question)
        )["query_constraints"]
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("recipe constraint extraction", exc) from exc
    extract_seconds = time.perf_counter() - extract_started

    search_intent = constraints.get("search_intent") or "constraints"
    title_only = search_intent == "title"
    title_query = (
        constraints.get("title_query")
        if search_intent in {"title", "title_with_constraints"}
        else None
    )
    requested_ingredients, resolved_allergens = (
        resolve_ingredient_allergen_conflicts(
            question=question,
            requested_ingredients=(
                constraints.get("preferred_ingredients") or []
            ),
            inferred_allergens=constraints.get("allergens") or [],
            explicit_allergens=exclude_allergens,
        )
    )

    # Diet asked for in the question is a hard filter. Member preferences are
    # soft boosts, while allergies remain hard exclusions.
    base_constraints = dict(
        include_ingredients=(
            [] if title_only else requested_ingredients
        ),
        exclude_ingredients=(
            [] if title_only else constraints.get("excluded_ingredients") or []
        ),
        exclude_allergens=(
            list(dict.fromkeys(exclude_allergens))
            if title_only
            else resolved_allergens
        ),
        diet_tags=[] if title_only else constraints.get("diet") or [],
        dish_types=[] if title_only else constraints.get("dish_types") or [],
        boost_tags=payload.diet_tags,
        boost_ingredients=payload.preferred_ingredients,
        title_keywords=(
            [] if title_only else constraints.get("title_keywords") or []
        ),
        title_query=title_query,
        # Always carried, never filters. Keeps relevance ordering even when the
        # extractor reduced the question to filters and dropped the dish noun.
        rank_query=question,
        max_duration_minutes=(
            None if title_only else constraints.get("max_duration_minutes")
        ),
        min_servings=(
            None if title_only else constraints.get("min_servings")
        ),
        sort_by=None if title_only else constraints.get("sort_by"),
        # An explicit page size wins over the extractor's. A paging client knows
        # how many rows it is rendering; the extractor is guessing from prose.
        limit=payload.limit or constraints.get("limit") or 10,
        offset=payload.offset or 0,
        region=payload.region or "eu",
        include_disabled=payload.include_disabled,
        # Facets for the question path too.
        #
        # Without them the filter panel keeps whatever counts the last
        # parameter search produced, so typing a query leaves chips advertising
        # totals for a different result set — clicking "Italian 1175" then
        # returns 9. Aggregations over a 7k-document index are not a meaningful
        # cost next to the constraint-extraction LLM call this path already
        # makes.
        include_facets=True,
    )
    # Reclassify course words the extractor mistook for ingredients.
    #
    # "vegan dessert" came back as include_ingredients=["dessert"], demanding a
    # recipe with an ingredient literally called "dessert" — zero matches,
    # where the corpus holds 360 vegan desserts. A word naming a course is a
    # course, not an ingredient, and the catalog registry already knows which
    # words those are.
    if base_constraints["include_ingredients"]:
        kept_ingredients: list[str] = []
        promoted: list[str] = []
        for ingredient in base_constraints["include_ingredients"]:
            canonical = canonical_course_type(ingredient)
            if canonical:
                promoted.append(canonical)
            else:
                kept_ingredients.append(ingredient)
        if promoted:
            base_constraints["include_ingredients"] = kept_ingredients
            base_constraints["dish_types"] = list(
                dict.fromkeys([*base_constraints["dish_types"], *promoted])
            )
            logger.info(
                "recipe_search reclassified %s from ingredients to course types",
                promoted,
            )

    # Recover course words the extractor dropped from the question entirely.
    #
    # The extractor reliably finds diet and ingredients but frequently discards
    # the noun naming the dish, and a word it never emitted cannot be rescued by
    # the reclassification above. The result was that "a light summer salad"
    # returned a crumble and "comfort food for a cold evening" returned Angel
    # Food Cake — `rank_query` matching "light summer" and "food" lexically
    # because nothing constrained the course.
    #
    # Scanning the question for course vocabulary is deterministic and cheap:
    # the words are a closed set, so this cannot invent a constraint the user
    # did not express.
    _question_tokens = re.findall(r"[a-z-]+", question.lower())

    if not base_constraints["dish_types"]:
        recovered: list[str] = []
        for token in _question_tokens:
            canonical = canonical_course_type(token)
            if canonical and canonical not in recovered:
                recovered.append(canonical)
        if recovered:
            base_constraints["dish_types"] = recovered
            logger.info(
                "recipe_search recovered course types %s from the question",
                recovered,
            )

    # Moods and cuisines, same treatment. The extractor's prompt predates both
    # vocabularies and never emits them, so "comfort food for a cold evening"
    # arrived with no mood at all and ranked Angel Food Cake first on the word
    # "food". Both vocabularies are closed sets, so scanning cannot invent a
    # constraint the user did not express.
    from recipe_wrangler.catalog import vocabularies as _V

    _mood_vocab = set(_V.MOODS)
    _cuisine_vocab = set(_V.CUISINES)
    _food_group_vocab = set(_V.FOOD_GROUPS)

    moods = [t for t in _question_tokens if t in _mood_vocab]
    cuisines = [t for t in _question_tokens if t in _cuisine_vocab]
    food_groups = [t for t in _question_tokens if t in _food_group_vocab]
    if moods:
        base_constraints["moods"] = list(dict.fromkeys(moods))
        logger.info("recipe_search recovered moods %s", base_constraints["moods"])
    if cuisines:
        base_constraints["cuisines"] = list(dict.fromkeys(cuisines))
        logger.info("recipe_search recovered cuisines %s", base_constraints["cuisines"])
    if food_groups:
        base_constraints["food_groups"] = list(dict.fromkeys(food_groups))
        logger.info(
            "recipe_search recovered food groups %s", base_constraints["food_groups"]
        )

    # Explicit caller selections override everything inferred above.
    #
    # The recovery passes exist because the extractor drops course, mood and
    # cuisine words from the question. They are guesses about intent. A facet
    # the caller clicked is not a guess, so it replaces the guess for that field
    # rather than being unioned with it — otherwise picking "Greek" on a search
    # for "italian pasta" would widen the results to both, which reads as the
    # filter being ignored.
    #
    # Applied before the lexical fallback so that a selection counts as a
    # signal: "pasta" plus a Greek cuisine chip should filter to Greek, not
    # decide that nothing was extracted.
    for _field in ("dish_types", "sources", "cuisines", "moods",
                   "flavor_profiles", "food_groups", "convenience",
                   "nutrition_claims", "nutri_scores"):
        _selected = getattr(payload, _field, None) or []
        if _selected:
            base_constraints[_field] = list(dict.fromkeys(_selected))

    # Diet needs its own field because `payload.diet_tags` already means
    # something else here — the member's profile groups, applied as boosts. A
    # diet the caller ticked is a requirement, so it lands on the hard filter
    # and joins whatever the question itself stated rather than replacing it:
    # "vegan" in the box and "gluten-free" on the chip means both.
    if payload.require_diet_tags:
        base_constraints["diet_tags"] = list(
            dict.fromkeys([*base_constraints["diet_tags"], *payload.require_diet_tags])
        )

    # Lexical fallback.
    #
    # The extractor classifies a bare noun like "pasta" as search_intent
    # "constraints" and then extracts no constraints, so every lexical and
    # filtering field ends up empty. build_es_query then emits `must: []` with
    # no `should`, which matches the entire profiled corpus, and the sort falls
    # back to expert_recipe/source_rank — so *every* such query returns the same
    # list in the same order regardless of what was asked. Using the raw
    # question as the title query restores both a `should` clause and
    # `_score`-first ordering.
    #
    # Deliberately only when NOTHING was extracted: a question like "under 30
    # minutes" legitimately yields only a duration filter, and forcing the
    # question text in as a title query would add minimum_should_match=1 and
    # wrongly exclude everything that does not contain those words.
    _signal_keys = (
        "include_ingredients", "exclude_ingredients", "exclude_allergens",
        "diet_tags", "dish_types", "sources", "title_keywords", "title_query",
        "cuisines", "moods", "flavor_profiles", "food_groups", "convenience",
        "nutrition_claims", "nutri_scores",
        "max_duration_minutes", "min_servings", "sort_by",
    )
    lexical_fallback = not any(
        base_constraints.get(key) not in (None, [], "") for key in _signal_keys
    )
    if lexical_fallback:
        base_constraints["title_query"] = question
        title_query = question
        logger.info(
            "recipe_search lexical fallback engaged for %r "
            "(extractor returned no constraints)",
            question[:80],
        )

    es_started = time.perf_counter()
    relaxed = False
    try:
        es_out = await run_in_threadpool(
            search_recipes_es, RecipeSearchConstraints(**base_constraints)
        )
        if (
            not es_out["results"]
            and not title_query
            and base_constraints["title_keywords"]
        ):
            relaxed = True
            es_out = await run_in_threadpool(
                search_recipes_es,
                RecipeSearchConstraints(
                    **base_constraints,
                    title_match_any=True,
                ),
            )
        elif not es_out["results"] and title_query:
            # Demote the title requirement to a ranking signal.
            #
            # A title_query makes the title clauses mandatory
            # (minimum_should_match=1). When the extractor emits one *alongside*
            # other constraints the combination is often unsatisfiable —
            # "vegetarian curry" yielded include_ingredients=["curry"] AND
            # title_query="vegetarian curry", requiring a recipe both containing
            # an ingredient called "curry" and titled with both words. That is
            # zero recipes, where the diet filter alone matches 3,914.
            #
            # Dropping it to `rank_query` keeps the words influencing the order
            # without excluding anything, so the user gets the vegetarian
            # curries they asked for rather than an empty page.
            relaxed = True
            demoted = dict(base_constraints)
            demoted["title_query"] = None
            demoted["rank_query"] = question
            es_out = await run_in_threadpool(
                search_recipes_es, RecipeSearchConstraints(**demoted)
            )
    except ResultWindowExceededError as exc:
        # Deep paging past Elasticsearch's result window. A 400 telling the
        # client to narrow is honest; the alternative is a 503 blaming the
        # search cluster for a request it correctly refused.
        raise InvalidError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc
    logger.info(
        "recipe_search question=%r extract=%.2fs es=%.2fs results=%d "
        "personalized=%s relaxed=%s constraints=%s",
        question[:80],
        extract_seconds,
        time.perf_counter() - es_started,
        len(es_out["results"]),
        bool(payload.diet_tags or payload.preferred_ingredients or exclude_allergens),
        relaxed,
        json.dumps(
            {
                key: value
                for key, value in base_constraints.items()
                if value not in (None, [], "") and key not in ("region", "limit")
            }
        ),
    )
    return {
        "results": [_es_card(card) for card in es_out["results"]],
        "total": es_out.get("total", 0),
        "facets": es_out.get("facets", {}),
    }


@router.post(
    "/param_search",
    response_model=None,
    tags=["recipes"],
    summary="Deterministic parameter-based Elasticsearch recipe search",
)
def param_search(payload: RecipeSearchFilters) -> dict[str, Any]:
    """Run deterministic parameter-based recipe search and return results."""

    try:
        es_out = search_recipes_es(
            RecipeSearchConstraints(
                include_ingredients=payload.include_ingredients,
                exclude_ingredients=payload.exclude_ingredients,
                exclude_allergens=payload.exclude_allergens,
                diet_tags=payload.diet_tags,
                sources=payload.sources,
                dish_types=payload.dish_types,
                cuisines=payload.cuisines,
                moods=payload.moods,
                flavor_profiles=payload.flavor_profiles,
                food_groups=payload.food_groups,
                convenience=payload.convenience,
                nutrition_claims=payload.nutrition_claims,
                nutri_scores=payload.nutri_scores,
                region=payload.region,
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

    if region not in {"IE", "HU", "EU", "SI"}:
        region = "IE"

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
            allergen_fields = _analysis_allergen_fields(names)
            auto_allergens = allergen_fields["allergens"]
            auto_tags = list(infer_diet_tags(set(auto_allergens)))
        except Exception:
            allergen_fields = {"allergens": [], "allergen_evidence": []}
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
            "allergen_evidence": allergen_fields["allergen_evidence"],
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

    profile_names = [
        str(name).strip()
        for name in (profile_result.get("ingredient_names") or [])
        if str(name).strip()
    ]
    if not profile_names:
        profile_names = [
            str(item.get("name") or item.get("ingredient") or "").strip()
            for item in (profile_result.get("ingredients") or [])
            if isinstance(item, dict)
            and str(item.get("name") or item.get("ingredient") or "").strip()
        ]
    allergen_fields = _analysis_allergen_fields(profile_names)
    profile_result["allergens"] = allergen_fields["allergens"]
    profile_result["allergen_evidence"] = allergen_fields["allergen_evidence"]

    # Return the full chain output so clients can access all parsed/profiling fields.
    # Strip top-level None values — they represent unset pipeline state, not meaningful nulls.
    profile_result = {k: v for k, v in profile_result.items() if v is not None}
    return {"message": "Success", **profile_result}


# ---------------------------------------------------------------------------
# Recipe creation endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/url/preview",
    tags=["recipes"],
    summary="Preview structured recipe data from a public URL",
)
async def recipe_url_preview(payload: RecipeUrlRequest) -> dict[str, Any]:
    from recipe_wrangler.utils.recipe_url import RecipeUrlError, fetch_recipe_from_url

    try:
        return await asyncio.to_thread(fetch_recipe_from_url, payload.url)
    except RecipeUrlError as exc:
        raise DataError(detail=str(exc), extra={"title": "RecipeUrlError"}) from exc
    except requests.RequestException as exc:
        raise map_dependency_error("recipe source website", exc) from exc


@router.post(
    "/url/import",
    response_model=RecipeCreateResponse,
    tags=["recipes"],
    summary="Import and profile a recipe from a public URL",
)
async def recipe_url_import(
    payload: RecipeUrlRequest, caller: Caller = Depends(get_caller)
) -> RecipeCreateResponse:
    draft = await recipe_url_preview(payload)
    missing = list(draft.get("missing_required_fields") or [])
    if missing:
        raise DataError(
            detail=(
                "The source page does not provide fields required for a safe import: "
                + ", ".join(missing)
                + ". Preview it and supply/correct these fields through the normal create endpoint."
            ),
            extra={"title": "IncompleteRecipeUrl", "missing_fields": missing},
        )
    return await recipe_create(
        RecipeCreateRequest(
            title=draft["title"],
            ingredients=draft["ingredients"],
            instructions=draft["instructions"],
            duration=draft["duration"],
            serves=draft["serves"],
            region=payload.region,
            image_url=draft.get("image_url"),
            url=draft["url"],
        ),
        caller,
    )

def _generate_user_recipe_id(title: str, ingredients: list[str]) -> str:
    """Generate a UUID for a newly created user recipe."""
    _ = (title, ingredients)  # keep signature compatibility for existing call sites
    return str(uuid4())


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
async def recipe_create(
    payload: RecipeCreateRequest, caller: Caller = Depends(get_caller)
) -> RecipeCreateResponse:
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
    nutrition_source = _nutrition_source_from_region(region) or "irish"

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

        from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals

        nutrition_source_key = profile_result.get("nutrition_source_key") or nutrition_source
        totals = profile_result.get("profiling_totals") or {}
        clean_totals = _extract_clean_totals(totals, f"_{nutrition_source_key}")
        clean_per_serving = (
            {k: v / payload.serves for k, v in clean_totals.items()}
            if clean_totals else None
        )

        # Compute nutri_score_breakdown immediately (same logic as backfill).
        if clean_totals:
            try:
                prof_ingredients = profile_result.get("ingredients") or []
                nutri_score_breakdown = _build_nutri_score_breakdown(
                    clean_totals, prof_ingredients
                )
            except Exception:
                pass

    # --- Allergen resolution ---
    # Diet tags are not computed here: they depend on the *complete* allergen
    # set (keyword + FoodOn), which is only known once upsert_recipe_to_neo4j
    # has run its detection -- computing them from this pre-write guess would
    # silently ignore anything FoodOn finds that the keyword scan misses.
    auto_allergens = detect_allergens_from_names(ingredient_names)
    merged_allergens: list[str] = sorted(set(auto_allergens) | set(payload.allergens))

    # --- Neo4j write ---
    try:
        merged_allergens, merged_tags = upsert_recipe_to_neo4j(
            recipe_id=recipe_id,
            title=payload.title,
            ingredient_lines=payload.ingredients,
            ingredient_names=ingredient_names,
            measurements=measurements,
            instructions=payload.instructions,
            duration=float(payload.duration),
            serves=float(payload.serves),
            image_url=payload.image_url,
            url=payload.url,
            allergens=merged_allergens,
            user_tags=payload.tags,
            source="user",
            source_id=payload.source_id,
            expert_recipe=payload.expert_recipe,
            # Keycloak subject, established by wisefood-api. Set once and never
            # overwritten, and redacted from responses for non-expert callers.
            creator=caller.creator_id,
            seasonality=payload.seasonality,
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

    # Nutrition claims are deterministic facets, not model annotations. They
    # are written after the complete profile exists and before projection so a
    # newly created recipe enters Elasticsearch with the same claim vocabulary
    # as a corpus backfill. Manual totals have no recipe weight basis, so only a
    # genuinely calculable profile can produce per-100g claims.
    if profile_result:
        claims = compute_nutrition_claim_tags(
            clean_totals,
            profile_result.get("ingredients"),
            profile_result.get("nutri_score"),
            physical_form=infer_physical_form(payload.title, payload.tags),
        )
        try:
            replace_recipe_nutrition_claims(recipe_id, claims)
        except Exception:
            logger.warning(
                "could not persist nutrition claims for %s", recipe_id, exc_info=True
            )

    # Commit: project into the catalog index, then annotate.
    #
    # A created recipe is only finished when it is stored, profiled *and*
    # annotated. Profiling happened above; the remaining two are one call so
    # they cannot drift apart. Without annotation the recipe exists and is
    # searchable by text but carries no cuisine, mood, flavour or food group —
    # so it is unreachable by every discovery filter in the UI and invisible to
    # the meal planner's cuisine preferences. A recipe nobody can find is not
    # meaningfully created.
    #
    # Runs after the Postgres trace so nutri-scores land in the same document.
    # Partial outcomes are reported, never raised: the owners already committed,
    # and the pending markers `commit` leaves behind are what reconciliation and
    # the annotation backfill consume.
    commit_result = commit_recipe(recipe_id)
    if not commit_result.complete:
        logger.warning(
            "recipe %s created but incomplete — %s",
            recipe_id,
            commit_result.summary(),
        )

    projected: dict[str, Any] = {}
    try:
        projected = _catalog_recipe_by_id(recipe_id) or {}
    except Exception:
        logger.warning(
            "could not read projected allergen evidence for %s",
            recipe_id,
            exc_info=True,
        )
    response_evidence = list(projected.get("allergen_evidence") or [])
    response_allergens = list(projected.get("allergens") or [])
    if not response_evidence:
        response_evidence = detect_allergen_evidence_from_names(ingredient_names)
    if not response_allergens:
        response_allergens = sorted(
            {row["allergen"] for row in response_evidence} | set(merged_allergens)
        )
    return RecipeCreateResponse(
        recipe_id=recipe_id,
        allergens=response_allergens,
        allergen_evidence=response_evidence,
    )


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
    if region not in {"IE", "HU", "EU", "SI"}:
        region = "IE"

    # --- Fetch recipe ---
    try:
        recipe = _catalog_recipe_by_id(recipe_id)
    except Exception as exc:
        raise map_dependency_error("Elasticsearch", exc) from exc

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
    summary="Update mutable recipe fields across owner and search stores",
)
async def recipe_update(recipe_id: str, payload: RecipeUpdateRequest) -> RecipeUpdateResponse:
    """Patch mutable fields on an existing recipe.

    - **Neo4j**: updates the supplied owner fields on the Recipe node.
    - **Elasticsearch**: fully reprojects the recipe, including deterministic
      v4 facets such as convenience and source-provided seasonality.
    - **Postgres**: nutrition traces are not affected (they store nutrients, not content).

    Returns 404 if the recipe does not exist in Neo4j.
    """
    patchable = (
        "instructions", "image_url", "source_id", "expert_recipe", "title",
        "allergens", "tags", "duration", "seasonality",
    )
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
            seasonality=payload.seasonality,
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

    # --- Full-doc reprojection so title/tags/allergens/duration/expert_recipe
    # edits reach search instead of going stale until a rebuild.
    #
    # Goes through the same commit path as creation, so an edit cannot leave the
    # index in a state a creation could not. `annotate_recipe` is on but the
    # commit skips a recipe that already carries facets — so a recipe annotated
    # at creation is not re-classified on every edit (a model call per keystroke,
    # and a silent overwrite of anything a human confirmed), while one that was
    # never successfully annotated gets another attempt here.
    commit_recipe(resolved_cache_id or recipe_id)

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
    """Return the single live recipe-catalog alias."""
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
