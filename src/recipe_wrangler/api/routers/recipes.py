"""Recipe-related endpoints router."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, Query

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.exceptions import (
    InternalError,
    NotFoundError,
)
from recipe_wrangler.api.config import get_settings

from recipe_wrangler.tools.param_search import search_recipes_by_params
from recipe_wrangler.tools.fetch_recipe_info import (
    fetch_recipe_info,
    fetch_recipe_info_by_id,
)
from recipe_wrangler.repositories.neo4j_recipes import (
    fetch_recipe_image_urls_by_ids,
    fetch_recipe_scores_by_ids,
)
from recipe_wrangler.repositories.postgres_nutrition import (
    get_recipe_nutrition,
    get_recipe_profile_trace,
    save_recipe_profile_trace,
)
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent
from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain

from ..dependencies import get_recipe_search_app
from recipe_wrangler.schemas import (
    RecipeSearchRequest,
    RecipeProfileRequest,
    RecipeSearchFilters,
    RecipeDetailResponse,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


def _profile_meta() -> tuple[str, str, str, str]:
    settings = get_settings()
    return (
        settings.profile_pipeline_version,
        settings.profile_mapping_version,
        settings.profile_embedding_model,
        settings.profile_ruleset_version,
    )


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_dict(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


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
        if canonical_food_id:
            fvl_ingredients.append(
                {
                    "name": row.get("ingredient"),
                    "weight_grams": weight,
                    "usda_id": canonical_food_id,
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
        return {"suggestions": []}

    settings = get_settings()
    search_payload = {
        "size": limit,
        "_source": ["title"],
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
    suggestions: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        title = hit.get("_source", {}).get("title")
        if not isinstance(title, str):
            continue
        normalized = title.strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(normalized)

    return {"suggestions": suggestions}


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
        raise map_dependency_error("Neo4j", exc) from exc

    if not recipe:
        raise NotFoundError("Recipe not found")

    # A request can match either r.recipe_id or r.id. Nutrition/profile stores are keyed by
    # canonical recipe_id, so prefer the resolved recipe_id from Neo4j when available.
    resolved_recipe_id = str(recipe.get("recipe_id") or recipe_id)
    recipe["recipe_id"] = resolved_recipe_id

    nutrition = None
    try:
        nutrition = get_recipe_nutrition(resolved_recipe_id)
    except Exception:
        nutrition = None

    stored_trace = None
    try:
        stored_trace = get_recipe_profile_trace(resolved_recipe_id)
    except Exception:
        stored_trace = None

    if not nutrition and isinstance(stored_trace, dict):
        trace_totals = _as_dict(stored_trace.get("total_nutrients"))
        trace_per_serving = _as_dict(stored_trace.get("total_nutrients_per_serving"))
        if trace_totals or trace_per_serving:
            nutrition = {
                "total_nutrients": trace_totals,
                "total_nutrients_per_serving": trace_per_serving,
                "nutri_score": stored_trace.get("nutri_score"),
                "source": stored_trace.get("nutrition_source") or stored_trace.get("source"),
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
        total_nutrients = nutrition.get("total_nutrients")
        total_nutrients_per_serving = nutrition.get("total_nutrients_per_serving")
        nutrient_basis = (
            total_nutrients_per_serving
            if isinstance(total_nutrients_per_serving, dict)
            else total_nutrients
        )
        serves = payload.get("serves")
        payload.update(
            {
                "nutrients": _normalize_nutrients(nutrient_basis),
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
                "total_nutrients": total_nutrients,
                "total_nutrients_per_serving": total_nutrients_per_serving,
                "nutri_score_raw": nutrition.get("nutri_score"),
                "nutri_score_breakdown": (
                    (stored_trace or {}).get("nutri_score_breakdown")
                    if isinstance((stored_trace or {}).get("nutri_score_breakdown"), dict)
                    else _build_nutri_score_breakdown(total_nutrients, profile_details)
                ),
                "nutrition_source": (
                    nutrition.get("source")
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

    return RecipeDetailResponse(**payload)


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
    (
        profile_pipeline_version,
        profile_mapping_version,
        profile_embedding_model,
        profile_ruleset_version,
    ) = _profile_meta()

    trace_payload = {
        "recipe_id": recipe_id,
        "title": profile_result.get("title"),
        "source": profile_result.get("source"),
        "nutrition_source": profile_result.get("nutrition_source"),
        "total_nutrients": totals if isinstance(totals, dict) else None,
        "total_nutrients_per_serving": None,
        "nutri_score": profile_result.get("nutri_score"),
        "nutri_score_breakdown": profile_result.get("nutri_score_breakdown"),
        "nutrition_profiling_details": profile_result.get("ingredients"),
        "nutrition_profiling_debug": profile_result.get("pipeline_trace"),
        "trace": profile_result,
        "pipeline_version": profile_pipeline_version,
        "mapping_version": profile_mapping_version,
        "embedding_model": profile_embedding_model,
        "ruleset_version": profile_ruleset_version,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_recipe_profile_trace(trace_payload)
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

    # If no free-text question is provided, return a random unconstrained page
    # (same behavior style as /param_search with empty payload).
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

    return {"results": results}


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

    if region not in {"IE", "US"}:
        region = "US"

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
    return {"message": "Success", **profile_result}
