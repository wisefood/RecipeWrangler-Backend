# Purpose: Merge nutrition + sustainability results into a unified profile.

# recipe_profiling.py

from typing import Any, Dict

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_chroma
from recipe_wrangler.tools.sustainability_calculator import (
    sustainability_tool_chroma,
)
from recipe_wrangler.utils.nutri_score import (
    compute_nutri_score,
    compute_nutri_score_with_breakdown,
)

NUTRI_SCORE_SOURCE_URL = (
    "https://nutriscore.blog/2022/12/25/spreadsheet-to-calculate-the-updated-version-of-the-nutri-score/"
)

# --- accuracy guards ------------------------------------------------------- #
_SERVES_MIN = 1.0
_SERVES_GIVEN_MAX = 50.0        # a *given* serves up to this is accepted (cookies/muffins/etc.)
_SERVES_EST_MIN, _SERVES_EST_MAX = 1.0, 16.0  # an *estimated* serves is clamped to this
_SERVING_EST_G = 450.0          # rough grams/serving used to estimate missing serves
_PER_SERVING_TARGET_G = 700.0   # what a sanity-trimmed recipe is brought back to
_PER_SERVING_CEILING_G = 2500.0  # above this/serving the recipe is "implausibly inflated"
_LOW_COVERAGE_THRESHOLD = 0.80   # below this fraction of recipe weight matched -> flagged


def _sanitize_serves(parsed: Any, total_weight_g: float) -> tuple[float, str]:
    """Return (serves, source). 'given' if the parsed value is in [1, 50] (so a
    legitimate "makes 24 cookies" survives), else 'estimated' from total recipe
    weight at ~450 g/serving (clamped to [1, 16]). A wildly-large total weight
    (parse artefact) is *not* trusted — fall back to 4 and let the weight cap trim it."""
    try:
        v = float(parsed)
        if _SERVES_MIN <= v <= _SERVES_GIVEN_MAX:
            return float(round(v)), "given"
    except (TypeError, ValueError):
        pass
    if total_weight_g and 0 < total_weight_g <= _SERVES_EST_MAX * _SERVING_EST_G:
        est = max(_SERVES_EST_MIN, min(_SERVES_EST_MAX, round(total_weight_g / _SERVING_EST_G)))
        return float(est), "estimated"
    if total_weight_g and total_weight_g > 0:
        return 4.0, "estimated"
    return 1.0, "estimated"


def _cap_recipe_weights(weights: list[float], serves: float) -> tuple[list[float], bool]:
    """Trim an implausibly-inflated recipe (e.g. parse artefact '313 cups flour' -> 39 kg)
    so its total lands at a sane mass before it propagates to nutrition / Nutri-Score.
    Returns (weights, was_capped)."""
    w = [max(0.0, float(x)) for x in weights]
    total = sum(w)
    if total <= 0 or serves <= 0 or total <= serves * _PER_SERVING_CEILING_G:
        return w, False
    if w:
        biggest = max(range(len(w)), key=lambda i: w[i])
        if w[biggest] > 0.55 * total:
            # one ingredient dominates -> trim only it, down toward the target total
            trim = total - serves * _PER_SERVING_TARGET_G
            w[biggest] = max(w[biggest] * 0.05, w[biggest] - trim)
            return w, True
    # uniformly inflated -> scale everything down to the ceiling
    scale = (serves * _PER_SERVING_CEILING_G) / total
    return [x * scale for x in w], True


def _source_from_region(region: Any) -> str:
    region_norm = str(region or "IE").strip().upper()
    if region_norm == "IE":
        return "irish"
    if region_norm == "US":
        return "usda"
    if region_norm == "HU":
        return "hungarian"
    if region_norm == "EU":
        return "eu"
    raise ValueError(f"Unsupported region '{region_norm}'. Supported regions: IE, US, HU, EU")


def _resolve_nutrition_source(payload: Dict[str, Any]) -> str:
    explicit_source = str(payload.get("source") or "").strip().lower()
    if explicit_source:
        if explicit_source in {"irish", "usda", "hungarian", "eu"}:
            return explicit_source
        raise ValueError(
            f"Unsupported source '{explicit_source}'. Supported sources: irish, usda, hungarian, eu"
        )
    return _source_from_region(payload.get("region"))

def Recipe_Profiling_Tool(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run nutritional and sustainability profiling on a recipe
    and merge their results into a single dictionary.

    Args:
        payload (dict): The input dictionary with keys like
            - title (str)
            - ingredient_names (list[str])
            - measurements (list[str])
            - weights (list[float])
            - serving_size_g (float)
            - serves (int)
            - min_similarity (float)

    Returns:
        dict: Combined recipe profiling with nutritional + sustainability
              information merged per ingredient and recipe totals.
    """

    nutrition_payload = dict(payload)
    sustainability_payload = dict(payload)
    sustainability_payload.pop("source", None)

    nutrition_payload["source"] = _resolve_nutrition_source(nutrition_payload)
    nutrition_result = nutritional_tool_chroma.invoke(nutrition_payload)
    nutrition_source_key = nutrition_result.get("source_key", "unknown")
    sustainability_result = sustainability_tool_chroma.invoke(sustainability_payload)

    merged: Dict[str, Any] = {
        "title": payload.get("title", ""),
        "ingredients": [],
        "totals": {},
        "sustainability_per_kg": sustainability_result.get("sustainability_per_kg"),
        "sustainability_serves": sustainability_result.get("serves"),
        "sustainability_details": sustainability_result.get("details", []),
    }
    merged["nutrition_source_key"] = nutrition_source_key
    merged["nutrition_source"] = nutrition_result.get("source")

    nutrition_details = nutrition_result.get("details", [])
    sustainability_details = sustainability_result.get("details", [])

    for i, ingredient in enumerate(payload["ingredient_names"]):
        ingredient_entry = {"ingredient": ingredient}

        if i < len(nutrition_details):
            ingredient_entry.update(nutrition_details[i])

        if i < len(sustainability_details):
            for k, v in sustainability_details[i].items():
                if k == "ingredient":
                    continue
                if k not in ingredient_entry:
                    ingredient_entry[k] = v
                else:
                    ingredient_entry[f"sustainability_{k}"] = v

        merged["ingredients"].append(ingredient_entry)

    totals = {}
    for source, prefix in [
        (nutrition_result, "nutrition"),
        (sustainability_result, "sustainability"),
    ]:
        for k, v in source.items():
            if k.startswith("total_"):
                key = k if prefix in {"nutrition", "sustainability"} else f"{prefix}_{k}"
                totals[key] = v

    merged["totals"] = totals

    return merged


from typing import Any, Dict, List, cast
from recipe_wrangler.repositories.chroma_matchers import query_usda_nutrition_candidates

_USDA_MATCH_THRESHOLD = 0.4
_CLEAN_TOTAL_KEYS = [
    "protein_g", "carbohydrate_g", "fat_g", "energy_kcal",
    "sugar_g", "saturated_fat_g", "sodium_mg", "fibre_g",
]


def _extract_clean_totals(totals: Dict[str, Any], suffix: str) -> Dict[str, float] | None:
    """Return totals keyed by clean names (e.g. protein_g) regardless of input format."""
    # Prefer pre-built clean_totals emitted by nutritional_calculator
    clean = totals.get("clean_totals")
    if isinstance(clean, dict) and all(k in clean for k in _CLEAN_TOTAL_KEYS):
        return {k: float(clean[k]) for k in _CLEAN_TOTAL_KEYS}
    # Fall back to suffix keys (e.g. total_protein_g_irish)
    result: Dict[str, float] = {}
    for key in _CLEAN_TOTAL_KEYS:
        val = totals.get(f"total_{key}{suffix}")
        if val is None:
            return None
        result[key] = float(val)
    return result


def _resolve_fvl_usda_id(canonical_food_id: str | None, name: str | None) -> str | None:
    """Return a USDA NDB number for food-group classification (fruit% calculation)."""
    if canonical_food_id:
        s = str(canonical_food_id)
        if len(s) >= 2 and s[:2].isdigit():
            return s
    if name:
        try:
            candidates = query_usda_nutrition_candidates(name.strip())
            if candidates and candidates[0].get("distance", 1.0) < _USDA_MATCH_THRESHOLD:
                return candidates[0].get("metadata", {}).get("usda_id")
        except Exception:
            pass
    return None


def _build_total_nutrients_for_score(
    totals: Dict[str, float],
    suffix: str,
    serves: float,
) -> Dict[str, Any] | None:
    def _pick_total(metric: str) -> float | None:
        # Prefer clean key first, then suffix key, then per-serving * serves fallback.
        value = totals.get(metric)
        if value is not None:
            return float(value)
        total_key = f"total_{metric}{suffix}"
        value = totals.get(total_key)
        if value is not None:
            return float(value)
        per_serving_key = f"total_{metric}_per_serving{suffix}"
        per_serving = totals.get(per_serving_key)
        if per_serving is None:
            return None
        return float(per_serving) * float(serves)

    energy_kcal = _pick_total("energy_kcal")
    sugar_g = _pick_total("sugar_g")
    saturated_fat_g = _pick_total("saturated_fat_g")
    sodium_mg = _pick_total("sodium_mg")
    fibre_g = _pick_total("fibre_g")
    protein_g = _pick_total("protein_g")

    required = [energy_kcal, sugar_g, saturated_fat_g, sodium_mg, fibre_g, protein_g]
    if any(v is None for v in required):
        return None

    # Existing Nutri-Score helper expects "Energy" in kJ-oriented thresholds.
    energy_kj = float(energy_kcal) * 4.184
    return {
        "nutrients": {
            "Energy": {"value": energy_kj},
            "Sugars, total": {"value": float(sugar_g)},
            "Fatty acids, total saturated": {"value": float(saturated_fat_g)},
            "Sodium, Na": {"value": float(sodium_mg)},
            "Fiber, total dietary": {"value": float(fibre_g)},
            "Protein": {"value": float(protein_g)},
        }
    }


def _build_nutrition_summary(totals: Dict[str, Any], suffix: str, serves: float) -> Dict[str, Any]:
    """Return a flat dict with human-readable nutrient names and per-serving values."""
    def _get(key: str) -> float | None:
        v = totals.get(f"total_{key}{suffix}")
        if v is None:
            v = totals.get(f"total_{key}_per_serving{suffix}")
            if v is not None and serves > 0:
                return float(v)
        return float(v) if v is not None else None

    def _per_serving(key: str) -> float | None:
        v = totals.get(f"total_{key}_per_serving{suffix}")
        if v is None:
            total = _get(key)
            if total is not None and serves > 0:
                return total / serves
        return float(v) if v is not None else None

    return {
        "energy_kcal": _get("energy_kcal"),
        "energy_kcal_per_serving": _per_serving("energy_kcal"),
        "protein_g": _get("protein_g"),
        "protein_g_per_serving": _per_serving("protein_g"),
        "carbohydrate_g": _get("carbohydrate_g"),
        "carbohydrate_g_per_serving": _per_serving("carbohydrate_g"),
        "fat_g": _get("fat_g"),
        "fat_g_per_serving": _per_serving("fat_g"),
        "sugar_g": _get("sugar_g"),
        "sugar_g_per_serving": _per_serving("sugar_g"),
        "saturated_fat_g": _get("saturated_fat_g"),
        "saturated_fat_g_per_serving": _per_serving("saturated_fat_g"),
        "sodium_mg": _get("sodium_mg"),
        "sodium_mg_per_serving": _per_serving("sodium_mg"),
        "fibre_g": _get("fibre_g"),
        "fibre_g_per_serving": _per_serving("fibre_g"),
        "serves": serves,
        "nutrition_source": suffix.lstrip("_"),
    }


def Recipe_Profiling_Node(state: RecipeState) -> RecipeState:
    """
    ode that runs nutrition + sustainability profiling for the recipe and writes the merged ingredient details, 
    totals, and source info back into the flow state.
    """
    names: List[str] = state.ingredient_names or []
    measurements: List[str] = state.measurements or []
    raw_weights = state.weights or []
    if isinstance(raw_weights, dict):
        weights = raw_weights.get("weights") or []
    else:
        weights = raw_weights
    weights = [float(x) for x in weights]

    # accuracy guards: estimate/clamp serves, and trim an implausibly-inflated recipe.
    _trusted = getattr(state, "trusted_serves", None)
    serves, serves_source = _sanitize_serves(
        _trusted if _trusted else state.serves, sum(weights)
    )
    raw_total_g = sum(weights)
    weights, weights_capped = _cap_recipe_weights(weights, serves)

    region = (state.region or "IE").strip().upper()
    region_source = (
        "irish"
        if region == "IE"
        else ("usda" if region == "US"
              else ("hungarian" if region == "HU"
                    else ("eu" if region == "EU" else None)))
    )
    nutrition_source = (
        getattr(state, "nutrition_source", None)
        or getattr(state, "nutritional_source", None)
        or getattr(state, "source", None)
        or region_source
    )
    if not nutrition_source:
        raise ValueError(f"Unsupported region '{region}'. Supported regions: IE, US, HU")

    payload: Dict[str, Any] = {
        "title": state.title or "Untitled Recipe",
        "ingredient_names": names,
        "measurements": measurements,
        "weights": weights,
        "serving_size_g": state.serving_size_g
            or (sum(weights) / serves if weights else 0.0),
        "serves": serves,
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.5,
        "region": region,
        "source": nutrition_source,
    }

    directions: List[str] = list(state.directions or [])
    profile: Dict[str, Any] = Recipe_Profiling_Tool(payload)
    totals: Dict[str, float] = cast(Dict[str, float], profile.get("totals", {}))
    prof_items: List[Dict[str, Any]] = cast(List[Dict[str, Any]], profile.get("ingredients", []))
    nutrition_source_key = cast(str, profile.get("nutrition_source_key") or "unknown")
    suffix = f"_{nutrition_source_key}"
    nutri_score_payload: Dict[str, Any] | None = None
    nutri_score_breakdown: Dict[str, Any] | None = None
    score_input = _build_total_nutrients_for_score(totals, suffix, float(serves))
    if score_input:
        score_ingredients = []
        for i in range(min(len(names), len(weights), len(prof_items))):
            entry: Dict[str, Any] = {"name": names[i], "weight_grams": weights[i]}
            usda_id = _resolve_fvl_usda_id(
                prof_items[i].get("canonical_food_id"), names[i]
            )
            if usda_id:
                entry["usda_id"] = usda_id
            score_ingredients.append(entry)
        maybe_score = compute_nutri_score_with_breakdown(score_input, score_ingredients)
        if "error" not in maybe_score:
            nutri_score_breakdown = maybe_score.pop("breakdown", None)
            nutri_score_payload = maybe_score

    merged: List[Dict[str, Any]] = []
    n = min(len(names), len(measurements), len(weights), len(prof_items))
    for i in range(n):
        p = dict(prof_items[i])  # copy
        # unify field names: set canonical surface name + parser fields
        p["name"] = names[i]
        p["measurement"] = measurements[i]
        p["weight_g"] = float(weights[i])  # ensure numeric
        merged.append(p)

    per_serving_suffix = f"_per_serving{suffix}"

    total_sustainability = totals.get("total_sustainability")
    total_sustainability_per_serving = totals.get("total_sustainability_per_serving")

    # coverage: fraction of recipe weight that got a real nutrition / CO2e match
    _total_w = sum(float(p.get("weight_g") or 0.0) for p in merged) or 1.0
    _matched_w = sum(
        float(p.get("weight_g") or 0.0) for p in merged if p.get("matched_nutritional_ingredient")
    )
    nutrition_coverage = round(_matched_w / _total_w, 4)
    nutrition_low_coverage = nutrition_coverage < _LOW_COVERAGE_THRESHOLD
    _sus_details = profile.get("sustainability_details") or []
    _sus_matched_w = sum(
        float(d.get("weight_g") or 0.0) for d in _sus_details if d.get("cf_val") is not None
    )
    sustainability_coverage = round((_sus_matched_w / _total_w), 4) if _total_w else 0.0
    sustainability_low_coverage = sustainability_coverage < _LOW_COVERAGE_THRESHOLD
    quality_flags = {
        "serves_source": serves_source,
        "serves": serves,
        "weights_capped": weights_capped,
        "raw_total_weight_g": round(raw_total_g, 1),
        "capped_total_weight_g": round(sum(weights), 1),
        "nutrition_coverage": nutrition_coverage,
        "nutrition_low_coverage": nutrition_low_coverage,
        "sustainability_coverage": sustainability_coverage,
        "sustainability_low_coverage": sustainability_low_coverage,
    }

    out = {
        "ingredients": merged,

        # keep convenient totals (flattened)
        "profiling_totals": totals,
        "serves": serves,
        "serves_source": serves_source,
        "weights_capped": weights_capped,
        "nutrition_coverage": nutrition_coverage,
        "nutrition_low_coverage": nutrition_low_coverage,
        "sustainability_coverage": sustainability_coverage,
        "sustainability_low_coverage": sustainability_low_coverage,
        "profiling_quality": quality_flags,
        "total_sustainability": total_sustainability,
        "total_sustainability_per_serving": total_sustainability_per_serving,
        "sustainability_per_kg": profile.get("sustainability_per_kg"),
        f"total_carbohydrate_g{per_serving_suffix}": totals.get(f"total_carbohydrate_g{per_serving_suffix}"),
        f"total_fat_g{per_serving_suffix}": totals.get(f"total_fat_g{per_serving_suffix}"),
        f"total_protein_g{per_serving_suffix}": totals.get(f"total_protein_g{per_serving_suffix}"),
        f"total_energy_kcal{per_serving_suffix}": totals.get(f"total_energy_kcal{per_serving_suffix}"),
        "nutrition_source": profile.get("nutrition_source") or nutrition_source,
        "nutrition_source_key": nutrition_source_key,
        "nutri_score": nutri_score_payload,
        "nutri_score_breakdown": nutri_score_breakdown,
        "nutri_score_color": None if not nutri_score_payload else nutri_score_payload.get("color"),
        "nutri_score_source": NUTRI_SCORE_SOURCE_URL,
        "sustainability_profiling_details": profile.get("sustainability_details"),

        # keep entire tool output (optional, handy for debugging)
        "full_profile": {
            **profile,
            "directions": directions,
            "nutrition_summary": _build_nutrition_summary(totals, suffix, serves),
            "nutri_score": nutri_score_payload,
            "nutri_score_breakdown": nutri_score_breakdown,
            "nutri_score_source": NUTRI_SCORE_SOURCE_URL,
            "profiling_quality": quality_flags,
            "sustainability_profiling_details": profile.get("sustainability_details"),
        },
    }
    for key, value in out.items():
        setattr(state, key, value)

    trace = dict(state.pipeline_trace or {})
    trace["profiling"] = {
        "source": out.get("nutrition_source"),
        "source_key": out.get("nutrition_source_key"),
        "totals": totals,
        "ingredients": prof_items,
        "quality": quality_flags,
        "nutri_score": nutri_score_payload,
        "nutri_score_breakdown": nutri_score_breakdown,
        "nutri_score_source": NUTRI_SCORE_SOURCE_URL,
        "sustainability_profiling_details": profile.get("sustainability_details"),
    }
    state.pipeline_trace = trace
    return state
