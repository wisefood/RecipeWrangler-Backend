"""Recipe adaptation service.

Reuses existing helpers (no modifications to upstream code):
  - `fetch_recipe_profiling_trace_by_id` from `utils.nutrition_postgres`
  - `nutritional_tool_vector` from `tools.nutritional_calculator`
  - `compute_nutri_score_breakdown_from_values` from `utils.nutri_score`
  - Neo4j helpers in this package's `neo4j_queries`
"""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Any

from fastapi import HTTPException

from recipe_wrangler.repositories.vector_matchers import query_vector_collection
from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
from recipe_wrangler.tools.nutrition_match import food_class
from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_vector
from recipe_wrangler.tools.sustainability_calculator import best_sustainability_match
from recipe_wrangler.utils.nutri_score import (
    compute_nutri_score_breakdown_from_values,
)
from recipe_wrangler.utils.nutrition_postgres import (
    fetch_recipe_profiling_trace_by_id,
    fetch_recipe_profiling_traces_by_id,
)

from .llm_judge import rerank_with_llm
from .neo4j_queries import (
    fetch_recipe_default_nutriscore,
    fetch_recipe_consumer_context,
    filter_suitable_ingredients,
    find_substitute_candidates,
    flavor_similarity,
    get_ingredient_allergens,
    has_any_substitution_path,
    resolve_graph_name,
)


def _authoritative_grade(recipe_id: str, breakdown: dict[str, Any]) -> str:
    """The recipe's ORIGINAL Nutri-Score wins over the profiling trace's.

    The live profiling pipeline re-matches free-text ingredients and can
    drift toward better grades on messy ingredient lists; adaptation must
    grade the current recipe — and gate improvements — against the default
    score, falling back to the trace only when no default exists.
    """
    try:
        default_score = fetch_recipe_default_nutriscore(recipe_id)
    except Exception:
        default_score = None
    if default_score:
        return _grade_letter(default_score)
    return _grade_letter(breakdown.get("nutri_score"))


REGION_TO_SOURCE = {
    "IE": "irish",
    "HU": "hungarian",
    "EU": "eu",
    "SI": "slovenian",
}

MIN_TARGET_POINTS = 3
NUTRI_SCORE_MAX_NEGATIVE_POINTS = 10
CANDIDATE_MIN_SIMILARITY = 0.7
CONSUMER_CANDIDATE_POOL_SIZE = 50

# Letter rank for grade comparison: lower is better.
_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _grade_rank(letter: str) -> int:
    return _GRADE_RANK.get(letter, 99)


# Food-class pairs that are interchangeable for substitution despite differing.
# Fats cross the dairy/oil_fat line (butter ↔ margarine ↔ oil); everything else
# must match its own class. Catches cross-category nonsense (sugar→oil,
# butter→chocolate chips) deterministically, no LLM needed.
_CLASS_COMPATIBILITY = {
    frozenset({"dairy", "oil_fat"}),
}


def _food_class_compatible(original: str, candidate: str) -> bool:
    """True if ``candidate`` is a plausible same-/compatible-class swap for ``original``.

    Lenient when either side can't be classified (returns True) so we never block
    on a missing signal — the guard only fires on a confident class mismatch.
    """

    oc = food_class(original)
    cc = food_class(candidate)
    if not oc or not cc or oc == cc:
        return True
    return frozenset({oc, cc}) in _CLASS_COMPATIBILITY

# Maps Nutri-Score breakdown's negative-item key → per-ingredient detail keys
# and the input key expected by `compute_nutri_score_breakdown_from_values`.
# Energy is special: per-ingredient stores kcal, pyNutriScore expects kJ.
NUTRIENT_MAP: dict[str, dict[str, Any]] = {
    "energy": {
        "label": "energy",
        "abs_key": "energy_kcal",
        "per100g_key": "energy_kcal_per_100g",
        "ns_input_key": "energy",
        "unit_to_kj": True,
        "unit": "kcal",
    },
    "sugar": {
        "label": "sugars",
        "abs_key": "sugar_g",
        "per100g_key": "sugars_per_100g",
        "ns_input_key": "sugar",
        "unit_to_kj": False,
        "unit": "g",
    },
    "saturated_fats": {
        "label": "saturated fat",
        "abs_key": "saturated_fat_g",
        "per100g_key": "saturated_fat_per_100g",
        "ns_input_key": "saturated_fats",
        "unit_to_kj": False,
        "unit": "g",
    },
    "sodium": {
        "label": "sodium",
        "abs_key": "sodium_mg",
        "per100g_key": "sodium_per_100g_mg",
        "ns_input_key": "sodium",
        "unit_to_kj": False,
        "unit": "mg",
    },
}

# Full set of per-ingredient nutrient keys needed to recompute the score.
ALL_INGREDIENT_KEYS = [
    ("energy_kcal", "energy_kcal_per_100g"),
    ("carbs_g", "carbs_per_100g"),
    ("fat_g", "fat_per_100g"),
    ("sugar_g", "sugars_per_100g"),
    ("saturated_fat_g", "saturated_fat_per_100g"),
    ("sodium_mg", "sodium_per_100g_mg"),
    ("fibre_g", "fibre_per_100g"),
    ("protein_g", "protein_per_100g"),
]


def _region_to_source(region: str) -> str:
    src = REGION_TO_SOURCE.get(region.upper())
    if not src:
        raise HTTPException(status_code=422, detail=f"Unsupported region: {region}")
    return src


def _grade_letter(nutri_score_grade: Any) -> str:
    """`Nutriscore_C` → `C`. Also passes through a bare `C`."""
    if not nutri_score_grade:
        return "?"
    s = str(nutri_score_grade)
    if s.startswith("Nutriscore_"):
        return s.split("_", 1)[1]
    return s.upper()


def _load_profile(recipe_id: str, region: str) -> dict[str, Any]:
    source = _region_to_source(region)
    row = fetch_recipe_profiling_trace_by_id(recipe_id, nutrition_source=source)
    if not row:
        # Fall back to whatever region the recipe *was* profiled in, exactly as
        # the recipe-detail endpoint does when the requested region has no row.
        #
        # Without this the two disagreed: a Slovenian recipe asked for in US
        # rendered fine (detail falls back to its `eu` profile) but every
        # adaptation call 404d, so the "Improve" button appeared on recipes it
        # could never work for. Coverage is per (recipe, region) and is
        # genuinely partial, so an exact-match requirement here is a promise
        # the corpus does not keep.
        rows = fetch_recipe_profiling_traces_by_id(recipe_id) or []
        row = next((candidate for candidate in rows if candidate), None)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No profile found for recipe '{recipe_id}' in region '{region}'. "
                   "Profile the recipe first.",
        )
    if not row.get("nutri_score_breakdown"):
        raise HTTPException(
            status_code=404,
            detail=f"Profile for '{recipe_id}' (region '{region}') has no nutri_score_breakdown. "
                   "Re-profile the recipe.",
        )
    if not row.get("nutrition_profiling_details"):
        raise HTTPException(
            status_code=404,
            detail=f"Profile for '{recipe_id}' (region '{region}') has no nutrition_profiling_details.",
        )
    return row


def _profile_source(row: dict[str, Any], region: str) -> str:
    """The nutrition source the loaded profile row was actually computed with.

    Call sites used to re-derive this from the region, which was fine only while
    `_load_profile` refused anything but an exact regional match. Now that it
    falls back, deriving it from the region again would recompute per-ingredient
    details against one region's tables while `nutri_score_breakdown` on the same
    row came from another's — a breakdown and a detail list that disagree.
    """
    stored = str(row.get("nutrition_source") or "").strip().lower()
    return stored or _region_to_source(region)


def _recompute_ingredient_details(
    row: dict[str, Any],
    source: str,
) -> list[dict[str, Any]]:
    """Re-derive per-ingredient details with the full nutrient key set.

    The persisted `nutrition_profiling_details` rows on the dominant pipeline
    (`recompute_2026-05-11`) only store macros (fat_g / carbs_g / protein_g),
    not the breakdown-relevant fields (saturated_fat_g, sugar_g, sodium_mg,
    fibre_g, energy_kcal). We re-run the existing nutritional tool here using
    the stored (name, weight_g) pairs so the per-ingredient contributions are
    available for step 2 onwards. Title is best-effort from the row.
    """

    persisted = row.get("nutrition_profiling_details") or []
    selected: list[dict[str, Any]] = []
    for d in persisted:
        # The persisted shape uses "name" (renamed from upstream "ingredient").
        name = (d.get("name") or d.get("ingredient") or "").strip()
        w = d.get("weight_g")
        if not name or w is None:
            continue
        try:
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if wf <= 0:
            continue
        selected.append({"name": name, "weight_g": wf, "persisted": d})

    if not selected:
        raise HTTPException(
            status_code=422,
            detail=(
                "Profile has no usable (name, weight_g) pairs in "
                "nutrition_profiling_details — cannot derive ingredient contributions."
            ),
        )

    result = nutritional_tool_vector.invoke({
        "title": row.get("title") or "recipe",
        "ingredient_names": [s["name"] for s in selected],
        "weights": [s["weight_g"] for s in selected],
        "min_similarity": CANDIDATE_MIN_SIMILARITY,
        "source": source,
        "serves": 1.0,
    })
    details = result.get("details") or []
    if len(details) != len(selected):
        raise HTTPException(
            status_code=422,
            detail="nutritional_tool_vector returned a mismatched detail count.",
        )

    # Attach a Neo4j-resolved name to each detail so downstream graph queries
    # work on the everyday name rather than the FCT canonical row.
    for det, src in zip(details, selected):
        hints = []
        sust = src["persisted"].get("sustainability_ingredient") or src["persisted"].get("matched_sustainability_ingredient")
        if sust:
            hints.append(str(sust))
        det["graph_name"] = resolve_graph_name(det.get("ingredient") or src["name"], hints)
    return details


# Member dietary-goal slugs (e.g. FoodScholar writes properties.dietary_goals
# entries like "reduce_fat") normalized to NUTRIENT_MAP keys.
_GOAL_NUTRIENT_ALIASES: dict[str, str] = {
    "energy": "energy", "calories": "energy",
    "reduce_calories": "energy", "reduce_energy": "energy", "low_calorie": "energy",
    "sugar": "sugar", "sugars": "sugar", "reduce_sugar": "sugar", "low_sugar": "sugar",
    "saturated_fats": "saturated_fats", "saturated_fat": "saturated_fats",
    "fat": "saturated_fats", "reduce_fat": "saturated_fats", "low_fat": "saturated_fats",
    "sodium": "sodium", "salt": "sodium", "reduce_salt": "sodium", "low_salt": "sodium",
    "reduce_sodium": "sodium", "low_sodium": "sodium",
}


def _normalize_goal_nutrients(goals: list[str] | None) -> list[str]:
    """Map goal slugs/nutrient names to NUTRIENT_MAP keys, order-preserving."""
    normalized: list[str] = []
    for goal in goals or []:
        key = _GOAL_NUTRIENT_ALIASES.get(str(goal or "").strip().lower())
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def _identify_target_nutrient(
    breakdown: dict[str, Any],
    preferred_keys: list[str] | None = None,
) -> dict[str, Any] | None:
    """Step 1: pick the highest-scoring negative nutrient (≥ MIN_TARGET_POINTS).

    When the member has dietary goals (preferred_keys), the best-ranked
    nutrient matching a goal wins — provided it still clears
    MIN_TARGET_POINTS. Goals bias the choice; they never force a target the
    recipe doesn't actually score badly on.
    """

    items = (breakdown.get("negative_points") or {}).get("items") or {}
    ranked = sorted(
        (
            (key, item.get("points") or 0, item.get("value_per_100g") or 0.0, item.get("unit"))
            for key, item in items.items()
            if key in NUTRIENT_MAP
        ),
        key=lambda t: t[1],
        reverse=True,
    )
    if not ranked:
        return None
    top_key, top_points, top_value, top_unit = ranked[0]
    if preferred_keys:
        for key, points, value, unit in ranked:
            if key in preferred_keys and points >= MIN_TARGET_POINTS:
                top_key, top_points, top_value, top_unit = key, points, value, unit
                break
    if top_points < MIN_TARGET_POINTS:
        return None
    meta = NUTRIENT_MAP[top_key]
    return {
        "score_key": top_key,
        "label": meta["label"],
        "abs_key": meta["abs_key"],
        "per100g_key": meta["per100g_key"],
        "ns_input_key": meta["ns_input_key"],
        "unit_to_kj": meta["unit_to_kj"],
        "unit": top_unit or meta["unit"],
        "points": int(top_points),
        "current_value_per_100g": float(top_value),
    }


def _rank_offender_candidates(
    details: list[dict[str, Any]],
    target: dict[str, Any],
    require_substitutes: bool = True,
) -> list[dict[str, Any]]:
    """Step 2: rank ingredients by absolute contribution to the target nutrient.

    Returns every ingredient in descending contribution order. The orchestrator
    walks this list until one yields a candidate that actually improves the
    target. With ``require_substitutes`` (default), only ingredients that have a
    graph substitution path are kept — for swap modes. Reduce-quantity mode
    passes ``require_substitutes=False`` since any ingredient can be reduced.
    """

    abs_key = target["abs_key"]
    contributions = []
    total = 0.0
    for d in details:
        v = float(d.get(abs_key) or 0.0)
        total += v
        contributions.append((d, v))
    contributions.sort(key=lambda t: t[1], reverse=True)

    out: list[dict[str, Any]] = []
    for det, contrib in contributions:
        recipe_name = (det.get("ingredient") or "").strip()
        graph_name = det.get("graph_name")
        if not recipe_name or contrib <= 0:
            continue
        if require_substitutes and (not graph_name or not has_any_substitution_path(graph_name)):
            continue
        out.append({
            "name": recipe_name,
            "graph_name": graph_name,
            "weight_g": float(det.get("weight_g") or 0.0),
            "contribution": contrib,
            "contribution_pct": (contrib / total) if total else 0.0,
            "original_per_100g": float(det.get(target["per100g_key"]) or 0.0),
            "detail": det,
            "total_target_contribution": total,
        })
    return out


def _fetch_candidate_profile(candidate_name: str, source: str) -> dict[str, Any] | None:
    """Run the existing per-ingredient nutrition pipeline at 100g for one candidate.

    Cached: candidate names repeat heavily across recipes and requests (cream,
    butter, yoghurt, ...), and each lookup costs several vector/Postgres
    round-trips — the dominant share of a suggestions call's latency.
    """
    det = _fetch_candidate_profile_cached(
        str(candidate_name or "").strip().lower(), str(source or "")
    )
    return dict(det) if det is not None else None


@lru_cache(maxsize=1024)
def _fetch_candidate_profile_cached(candidate_name: str, source: str) -> dict[str, Any] | None:
    try:
        result = nutritional_tool_vector.invoke({
            "title": candidate_name,
            "ingredient_names": [candidate_name],
            "weights": [100.0],
            "min_similarity": CANDIDATE_MIN_SIMILARITY,
            "source": source,
            "serves": 1.0,
        })
    except Exception:
        return None
    details = result.get("details") or []
    if not details:
        return None
    det = details[0]
    if not det.get("matched_nutritional_ingredient"):
        return None
    return det


def _candidate_per_100g_map(detail: dict[str, Any]) -> dict[str, float]:
    """Extract the per-100g values from a nutritional_tool_vector detail row."""

    return {
        per100g_key: float(detail.get(per100g_key) or 0.0)
        for _abs, per100g_key in ALL_INGREDIENT_KEYS
    }


def _recipe_per_100g(
    details: list[dict[str, Any]],
    swap_original_name: str | None = None,
    swap_weight_g: float = 0.0,
    swap_candidate_per_100g: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, float], float]:
    """Sum per-ingredient absolutes, optionally replacing one row with a candidate.

    Returns (totals_absolute, per_100g, total_weight_g).
    """

    totals = {abs_k: 0.0 for abs_k, _ in ALL_INGREDIENT_KEYS}
    total_weight = 0.0
    swap_lower = swap_original_name.strip().lower() if swap_original_name else None
    swap_applied = False

    for d in details:
        name = (d.get("ingredient") or "").strip()
        weight = float(d.get("weight_g") or 0.0)
        if swap_lower and not swap_applied and name.lower() == swap_lower:
            # Replace original with candidate at the same (or overridden) weight.
            new_weight = swap_weight_g or weight
            scale = new_weight / 100.0
            for abs_k, per100g_k in ALL_INGREDIENT_KEYS:
                totals[abs_k] += scale * float((swap_candidate_per_100g or {}).get(per100g_k) or 0.0)
            total_weight += new_weight
            swap_applied = True
            continue
        for abs_k, _ in ALL_INGREDIENT_KEYS:
            totals[abs_k] += float(d.get(abs_k) or 0.0)
        total_weight += weight

    if total_weight <= 0:
        return totals, {abs_k: 0.0 for abs_k, _ in ALL_INGREDIENT_KEYS}, 0.0

    per_100g = {
        abs_k: (totals[abs_k] / total_weight) * 100.0
        for abs_k, _ in ALL_INGREDIENT_KEYS
    }
    return totals, per_100g, total_weight


def _ns_inputs_from_per_100g(
    per_100g: dict[str, float],
    fvl_pct: float,
) -> dict[str, float]:
    """Build the dict that compute_nutri_score_breakdown_from_values expects."""

    return {
        "energy": float(per_100g.get("energy_kcal", 0.0)) * 4.184,  # kcal → kJ
        "sugar": float(per_100g.get("sugar_g", 0.0)),
        "saturated_fats": float(per_100g.get("saturated_fat_g", 0.0)),
        "sodium": float(per_100g.get("sodium_mg", 0.0)),
        "fibers": float(per_100g.get("fibre_g", 0.0)),
        "proteins": float(per_100g.get("protein_g", 0.0)),
        "fruit_percentage": float(fvl_pct or 0.0),
    }


def _fvl_pct_from_breakdown(breakdown: dict[str, Any]) -> float:
    items = (breakdown.get("positive_points") or {}).get("items") or {}
    fvl = items.get("fruit_percentage") or {}
    return float(fvl.get("value_per_100g") or 0.0)


def _serves_from_row(row: dict[str, Any]) -> float:
    """Derive serves; fall back to ratio of totals/per-serving if needed."""

    quality = (row.get("nutrition_profiling_debug") or {}).get("profiling_quality") or {}
    s = quality.get("serves")
    if s is not None:
        try:
            sv = float(s)
            if sv > 0:
                return sv
        except (TypeError, ValueError):
            pass
    totals = row.get("total_nutrients") or {}
    per_serving = row.get("total_nutrients_per_serving") or {}
    for key in totals:
        try:
            t = float(totals[key])
            ps = float(per_serving.get(key, 0.0))
            if t > 0 and ps > 0:
                return t / ps
        except (TypeError, ValueError, KeyError):
            continue
    return 1.0


def _evaluate_candidate(
    candidate: dict[str, Any],
    offender: dict[str, Any],
    target: dict[str, Any],
    details: list[dict[str, Any]],
    fvl_pct: float,
    current_breakdown: dict[str, Any],
    source: str,
    original_allergens: set[str],
    serves: float,
    current_grade: str | None = None,
) -> dict[str, Any] | None:
    """Fetch, filter, simulate, score, and rank a single candidate."""

    # Food-class guard: reject cross-category swaps (e.g. sugar→oil) up front.
    if not _food_class_compatible(offender["name"], candidate["name"]):
        return None

    profile = _fetch_candidate_profile(candidate["name"], source)
    if not profile:
        return None
    cand_per_100g = _candidate_per_100g_map(profile)
    cand_target_per_100g = float(profile.get(target["per100g_key"]) or 0.0)
    # The nutrition pipeline zero-fills nutrients its FCT match lacks, so an
    # exact 0.0 on the TARGET nutrient is far more often a data gap than a
    # real value (e.g. "sour cream: 0.0g saturated fat"). A suggestion built
    # on a hole in the data overstates its benefit and erodes trust — reject;
    # near-zero genuine alternatives still rank on top.
    if cand_target_per_100g <= 0.0:
        return None
    original_per_100g_val = offender["original_per_100g"]
    if original_per_100g_val <= 0 or cand_target_per_100g >= original_per_100g_val:
        return None

    # Simulate the swap with the original weight (no override on /suggestions).
    _new_totals, new_per_100g, _weight = _recipe_per_100g(
        details,
        swap_original_name=offender["name"],
        swap_weight_g=offender["weight_g"],
        swap_candidate_per_100g=cand_per_100g,
    )
    ns_inputs = _ns_inputs_from_per_100g(new_per_100g, fvl_pct)
    try:
        new_breakdown = compute_nutri_score_breakdown_from_values(ns_inputs, "solid")
    except Exception:
        return None

    new_target_points = (
        ((new_breakdown.get("negative_points") or {}).get("items") or {})
        .get(target["score_key"], {})
        .get("points", target["points"])
    )
    points_saved = int(target["points"]) - int(new_target_points)
    if points_saved <= 0:
        return None

    # Strict grade-preservation gate: only accept candidates that improve the
    # overall letter grade. Saving points on the target nutrient isn't enough
    # if the swap drags other nutrients backward and the net grade stays flat
    # or worsens (e.g. butter→brown sugar drops sat fat but adds sugar).
    # Gate against the caller-supplied authoritative (default) grade when
    # given; the trace's own grade is only a fallback.
    if not current_grade:
        current_grade = _grade_letter(current_breakdown.get("nutri_score"))
    simulated_grade = _grade_letter(new_breakdown.get("nutri_score"))
    if _grade_rank(simulated_grade) >= _grade_rank(current_grade):
        return None

    # Per-serving delta (scaled by original weight / 100g) over all tracked nutrients.
    weight = offender["weight_g"]
    scale = weight / 100.0
    delta_per_serving = {}
    for abs_k, per100g_k in ALL_INGREDIENT_KEYS:
        original_contrib = float(offender["detail"].get(abs_k) or 0.0)
        candidate_contrib = scale * float(cand_per_100g.get(per100g_k) or 0.0)
        delta_per_serving[abs_k] = (candidate_contrib - original_contrib) / (serves or 1.0)

    cand_allergens = set(get_ingredient_allergens(candidate["name"]))
    new_allergens = sorted(cand_allergens - original_allergens)

    return {
        "candidate_name": candidate["name"],
        "source": candidate["source"],
        "category_distance": candidate["category_distance"],
        "candidate_per_100g_target": cand_target_per_100g,
        "original_per_100g_target": original_per_100g_val,
        "relative_improvement": (original_per_100g_val - cand_target_per_100g) / original_per_100g_val,
        "points_saved": points_saved,
        "new_breakdown": new_breakdown,
        "delta_per_serving": delta_per_serving,
        "introduces_allergen": bool(new_allergens),
        "new_allergens": new_allergens,
    }


def _build_explanation(
    target_label: str,
    target_points: int,
    original_name: str,
    original_contribution_g: float,
    serves: float,
    candidate_name: str,
    candidate_per_100g: float,
    original_per_100g: float,
    points_saved: int,
    current_grade: str,
    simulated_grade: str,
    new_allergens: list[str],
    unit: str,
) -> dict[str, Any]:
    per_serving_contribution = original_contribution_g / (serves or 1.0)
    fmt = "{:.1f}".format if unit != "kcal" else "{:.0f}".format
    warning = None
    if new_allergens:
        warning = "Introduces allergen(s): " + ", ".join(new_allergens) + "."
    return {
        "headline": f"Swap {original_name} → {candidate_name}",
        "reason": (
            f"{original_name.capitalize()} contributes {fmt(per_serving_contribution)}{unit} of "
            f"{target_label} per serving, costing this recipe {target_points} Nutri-Score points "
            f"out of {NUTRI_SCORE_MAX_NEGATIVE_POINTS}. "
            f"{candidate_name.capitalize()} has {fmt(candidate_per_100g)}{unit} per 100g compared "
            f"to {fmt(original_per_100g)}{unit}, saving {points_saved} points and improving the "
            f"grade from {current_grade} to {simulated_grade}."
        ),
        "warning": warning,
    }


# ---------- consumer-group adaptation helpers ----------


def _semantic_consumer_candidates(
    ingredient_name: str,
    group: str,
    recipe_title: str,
) -> list[dict[str, Any]]:
    """Retrieve global ingredient alternatives from Elasticsearch.

    The query encodes the requested adaptation goal (for example, ``vegan
    alternative to cheese``). Elasticsearch supplies recall and ranking only;
    Neo4j suitability remains the mandatory eligibility gate.
    """

    queries = [
        f"{group} alternative to {ingredient_name} for {recipe_title}",
        f"plant-based {ingredient_name}",
    ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query_number, query in enumerate(queries):
        try:
            hits = query_vector_collection(
                "ingredients",
                query,
                CONSUMER_CANDIDATE_POOL_SIZE,
            )
        except Exception:
            continue
        for rank, hit in enumerate(hits, start=1):
            metadata = hit.get("metadata") or {}
            name = str(
                metadata.get("name") or hit.get("document") or ""
            ).strip()
            key = name.casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "name": name,
                    "source": "elastic",
                    "category_distance": "high",
                    "retrieval_rank": rank + (
                        query_number * CONSUMER_CANDIDATE_POOL_SIZE
                    ),
                    "retrieval_score": float(hit.get("rrf_score") or 0.0),
                }
            )
    return candidates


_ALTERNATIVE_MARKER_RE = re.compile(
    r"\b(?:vegan|vegetarian|plant[\s-]*based|dairy[\s-]*free|"
    r"substitute|alternative|replacement|replacer)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_CHOICE_RE = re.compile(
    r"\b(?:or|and/or)\b|[/|]",
    re.IGNORECASE,
)
_MEASUREMENT_PREFIX_RE = re.compile(
    r"^\s*(?:\d|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]|"
    r"cups?\b|tablespoons?\b|teaspoons?\b|tbsp\b|tsp\b|"
    r"grams?\b|kilograms?\b|ounces?\b|pounds?\b|oz\b|lbs?\b)",
    re.IGNORECASE,
)


def _is_explicit_functional_alternative(
    original_name: str,
    candidate_name: str,
) -> bool:
    """Require an explicit role-preserving alternative, without item lists.

    Consumer safety and culinary equivalence are separate. A plant ingredient
    can be vegan-suitable without being a cheese/butter/egg replacement. This
    global gate keeps candidates that name the original role and explicitly
    describe themselves as an alternative, while rejecting ambiguous strings
    such as ``milk or plant-based milk``.
    """

    original = original_name.strip().casefold()
    candidate = candidate_name.strip().casefold()
    if (
        not original
        or not candidate
        or _AMBIGUOUS_CHOICE_RE.search(candidate)
        or _MEASUREMENT_PREFIX_RE.search(candidate)
    ):
        return False
    role_pattern = (
        r"(?<!\w)"
        + re.escape(original).replace(r"\ ", r"\s+")
        + r"(?!\w)"
    )
    return bool(
        re.search(role_pattern, candidate)
        and _ALTERNATIVE_MARKER_RE.search(candidate)
    )


def _is_simple_role_variant(
    original_name: str,
    candidate_name: str,
) -> bool:
    """Recognize concise role variants such as ``oat milk``.

    These are lower-confidence than explicit ``vegan ...`` alternatives and
    are used only when no explicit suitable candidate exists.
    """

    original = original_name.strip().casefold()
    candidate = candidate_name.strip().casefold()
    if (
        not original
        or not candidate
        or candidate == original
        or _AMBIGUOUS_CHOICE_RE.search(candidate)
        or _MEASUREMENT_PREFIX_RE.search(candidate)
    ):
        return False
    role_pattern = (
        r"(?<!\w)"
        + re.escape(original).replace(r"\ ", r"\s+")
        + r"(?!\w)$"
    )
    return bool(
        re.search(role_pattern, candidate)
        and len(re.findall(r"[a-z0-9]+", candidate)) <= 4
    )


def _functional_alternative_rank(
    original_name: str,
    candidate_name: str,
) -> tuple[int, int]:
    """Prefer the simplest explicit role-preserving alternative."""

    original = re.sub(r"\s+", " ", original_name.strip().casefold())
    candidate = re.sub(r"\s+", " ", candidate_name.strip().casefold())
    if candidate == f"vegan {original}":
        tier = 0
    elif candidate.startswith("vegan "):
        tier = 1
    elif candidate.startswith(("plant-based ", "plant based ", "dairy-free ")):
        tier = 2
    else:
        tier = 3
    return tier, len(re.findall(r"[a-z0-9]+", candidate))


def _consumer_candidate_pool(
    ingredient_name: str,
    group: str,
    existing_ingredients: set[str],
    recipe_title: str,
) -> list[dict[str, Any]]:
    """Union semantic and graph candidates, then enforce suitability.

    Semantic candidates lead because the goal-aware query reliably finds
    explicit alternatives such as ``vegan cheese``. MISKG/FoodOn candidates
    expand recall when those alternatives are absent. A candidate survives
    only if Neo4j explicitly says ``suitable`` for the requested group and it
    is not already present in the recipe.
    """

    semantic = _semantic_consumer_candidates(
        ingredient_name,
        group,
        recipe_title,
    )
    graph = find_substitute_candidates(ingredient_name)
    merged: dict[str, dict[str, Any]] = {}
    for position, candidate in enumerate(semantic + graph, start=1):
        name = str(candidate.get("name") or "").strip()
        key = name.casefold()
        if (
            not key
            or key == ingredient_name.strip().casefold()
            or key in existing_ingredients
        ):
            continue
        item = dict(candidate)
        item.setdefault("retrieval_rank", position)
        item.setdefault("retrieval_score", 0.0)
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
        elif previous.get("source") == "elastic" and item.get("source") in {
            "miskg",
            "foodon",
        }:
            # Keep the stronger goal-aware retrieval rank while exposing the
            # graph provenance when both systems found the same candidate.
            previous["source"] = item["source"]
            previous["category_distance"] = item["category_distance"]

    ordered = sorted(
        merged.values(),
        key=lambda item: int(item.get("retrieval_rank") or 10_000),
    )
    suitable = filter_suitable_ingredients(
        [item["name"] for item in ordered],
        group,
    )
    suitability_by_name = {
        item["name"].casefold(): item for item in suitable
    }

    result: list[dict[str, Any]] = []
    original_normalized = ingredient_name.strip().casefold()
    for item in ordered:
        evidence = suitability_by_name.get(item["name"].casefold())
        if evidence is None:
            continue
        candidate = {**item, **evidence}
        explicit_alternative = _is_explicit_functional_alternative(
            original_normalized,
            candidate["name"],
        )
        simple_role_variant = _is_simple_role_variant(
            original_normalized,
            candidate["name"],
        )
        if not explicit_alternative and not simple_role_variant:
            continue
        candidate["functional_name_match"] = True
        candidate["explicit_functional_alternative"] = explicit_alternative
        result.append(candidate)

    if any(item["explicit_functional_alternative"] for item in result):
        result = [
            item for item in result if item["explicit_functional_alternative"]
        ]
    result.sort(
        key=lambda item: (
            *_functional_alternative_rank(
                ingredient_name,
                item["name"],
            ),
            int(item.get("retrieval_rank") or 10_000),
            -float(item.get("retrieval_score") or 0.0),
        )
    )
    return result


_CONSUMER_SAFE_IDENTITY_RE = re.compile(
    r"\b(?:vegan|vegetarian|plant[\s-]*based|dairy[\s-]*free|non[\s-]*dairy|"
    r"meat[\s-]*free|egg[\s-]*free)\b",
    re.IGNORECASE,
)


def _nutrition_match_preserves_consumer_identity(
    candidate_name: str,
    original_name: str,
    candidate_profile: dict[str, Any],
    group: str,
) -> bool:
    """Reject nutrition matches that erase the consumer-safe identity.

    A high cosine score is insufficient: Hungarian ``vegan cheese`` currently
    retrieves ordinary ``cream cheese``. Explicit plant/vegan candidates must
    therefore match a nutrition record carrying equivalent plant-based
    evidence. Concise role variants such as ``oat milk`` must preserve at
    least one qualifier beyond the original role.
    """

    matched_name = str(
        candidate_profile.get("matched_nutritional_ingredient") or ""
    ).strip()
    if not matched_name:
        return False

    candidate = candidate_name.casefold()
    matched = matched_name.casefold()
    if group in {"vegan", "vegetarian"} and (
        _CONSUMER_SAFE_IDENTITY_RE.search(candidate)
    ):
        return bool(_CONSUMER_SAFE_IDENTITY_RE.search(matched))

    candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate))
    original_tokens = set(
        re.findall(r"[a-z0-9]+", original_name.casefold())
    )
    qualifier_tokens = candidate_tokens - original_tokens
    if not qualifier_tokens:
        return False
    matched_tokens = set(re.findall(r"[a-z0-9]+", matched))
    return bool(qualifier_tokens & matched_tokens)


def _find_profile_detail_for_graph_ingredient(
    details: list[dict[str, Any]],
    ingredient_name: str,
) -> dict[str, Any] | None:
    """Resolve a graph blocker to the corresponding profiled ingredient row."""

    target = re.sub(r"\s+", " ", ingredient_name.strip().casefold())
    if not target:
        return None
    target_tokens = set(re.findall(r"[a-z0-9]+", target))
    best: tuple[float, dict[str, Any]] | None = None
    for detail in details:
        fields = [
            detail.get("graph_name"),
            detail.get("ingredient"),
            detail.get("matched_nutritional_ingredient"),
        ]
        for field in fields:
            normalized = re.sub(
                r"\s+",
                " ",
                str(field or "").strip().casefold(),
            )
            if not normalized:
                continue
            if normalized == target:
                return detail
            field_tokens = set(re.findall(r"[a-z0-9]+", normalized))
            if not target_tokens or not target_tokens.issubset(field_tokens):
                continue
            score = len(target_tokens) / max(1, len(field_tokens))
            if best is None or score > best[0]:
                best = (score, detail)
    return best[1] if best else None


def _scaled_candidate_detail(
    candidate_name: str,
    candidate_profile: dict[str, Any],
    weight_g: float,
) -> dict[str, Any]:
    """Convert a candidate's 100 g profile into one weighted recipe row."""

    detail = dict(candidate_profile)
    detail["ingredient"] = candidate_name
    detail["graph_name"] = candidate_name
    detail["weight_g"] = float(weight_g)
    scale = float(weight_g) / 100.0
    for absolute_key, per_100g_key in ALL_INGREDIENT_KEYS:
        detail[absolute_key] = (
            float(detail.get(per_100g_key) or 0.0) * scale
        )
    return detail


def _nutrition_match_summary(
    candidate: dict[str, Any],
    candidate_profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "graph_ingredient": candidate["name"],
        "graph_recipe_usage_count": int(
            candidate.get("recipe_usage_count") or 0
        ),
        "matched_nutritional_ingredient": candidate_profile.get(
            "matched_nutritional_ingredient"
        ),
        "canonical_food_id": candidate_profile.get("canonical_food_id"),
        "source_nutrition": candidate_profile.get("source_nutrition"),
        "match_confidence": candidate_profile.get("match_confidence"),
        "similarity": candidate_profile.get("similarity"),
        "nutrients_per_100g": {
            per_100g_key: float(
                candidate_profile.get(per_100g_key) or 0.0
            )
            for _absolute_key, per_100g_key in ALL_INGREDIENT_KEYS
        },
    }


def _build_adapted_recipe_preview(
    graph_recipe: dict[str, Any],
    profile_row: dict[str, Any],
    profile_details: list[dict[str, Any]],
    offender_detail: dict[str, Any],
    original_name: str,
    candidate_name: str,
    candidate_profile: dict[str, Any],
    region: str,
    group: str,
    simulated_consumer_status: str,
) -> dict[str, Any]:
    """Return the full recipe preview and recalculated nutrition."""

    replacement_weight = float(offender_detail.get("weight_g") or 0.0)
    adapted_profile_details: list[dict[str, Any]] = []
    for detail in profile_details:
        if detail is offender_detail:
            adapted_profile_details.append(
                _scaled_candidate_detail(
                    candidate_name,
                    candidate_profile,
                    replacement_weight,
                )
            )
        else:
            adapted_profile_details.append(dict(detail))

    totals, per_100g, total_weight_g = _recipe_per_100g(
        adapted_profile_details
    )
    fvl_pct = _fvl_pct_from_breakdown(
        profile_row.get("nutri_score_breakdown") or {}
    )
    breakdown = compute_nutri_score_breakdown_from_values(
        _ns_inputs_from_per_100g(per_100g, fvl_pct),
        "solid",
    )
    serves = _serves_from_row(profile_row)
    divisor = serves if serves > 0 else 1.0
    per_serving = {
        key: value / divisor for key, value in totals.items()
    }

    adapted_ingredients: list[dict[str, Any]] = []
    for ingredient in graph_recipe.get("ingredients") or []:
        adapted = dict(ingredient)
        if (
            str(adapted.get("name") or "").strip().casefold()
            == original_name.strip().casefold()
        ):
            adapted["name"] = candidate_name
            adapted["replaces"] = original_name
        adapted_ingredients.append(adapted)

    return {
        "source_recipe_id": str(
            graph_recipe.get("recipe_id")
            or profile_row.get("recipe_id")
            or ""
        ),
        "title": graph_recipe.get("title") or profile_row.get("title"),
        "source": graph_recipe.get("source"),
        "url": graph_recipe.get("url"),
        "image_url": graph_recipe.get("image_url"),
        "duration": graph_recipe.get("duration"),
        "serves": graph_recipe.get("serves"),
        "instructions": list(graph_recipe.get("instructions") or []),
        "ingredients": adapted_ingredients,
        "adapted_for": group,
        "consumer_status": simulated_consumer_status,
        "nutrition": {
            "region": region.upper(),
            "nutrition_source": profile_row.get("nutrition_source"),
            "total_weight_g": total_weight_g,
            "effective_serves": divisor,
            "total_nutrients": totals,
            "total_nutrients_per_serving": per_serving,
            "total_nutrients_per_100g": per_100g,
            "nutri_score": _grade_letter(breakdown.get("nutri_score")),
            "nutri_score_breakdown": breakdown,
            "ingredient_details": adapted_profile_details,
        },
    }


def _consumer_explanation(
    group: str,
    original_name: str,
    candidate: dict[str, Any],
    remaining_blockers: list[str],
    unknown_ingredients: list[str],
    new_allergens: list[str],
) -> dict[str, Any]:
    warning_parts: list[str] = []
    if new_allergens:
        warning_parts.append(
            "Introduces allergen(s): " + ", ".join(new_allergens) + "."
        )
    if remaining_blockers:
        warning_parts.append(
            "The recipe still has other known blockers: "
            + ", ".join(remaining_blockers)
            + "."
        )
    if unknown_ingredients:
        warning_parts.append(
            "The following ingredients remain unverified: "
            + ", ".join(unknown_ingredients)
            + "; the adapted recipe cannot yet be labelled suitable."
        )
    result_statement = ""
    if not remaining_blockers and not unknown_ingredients:
        result_statement = (
            f" With no other blockers or unknown ingredients, this swap "
            f"would make the recipe compositionally {group}-suitable."
        )
    return {
        "headline": f"Swap {original_name} → {candidate['name']}",
        "reason": (
            f"{original_name.capitalize()} blocks the recipe's {group} "
            f"classification. {candidate['name'].capitalize()} is explicitly "
            f"classified as {group}-suitable under "
            f"{candidate['classification_version']} and was retrieved as a "
            f"possible functional alternative.{result_statement}"
        ),
        "warning": " ".join(warning_parts) or None,
    }


def _generate_consumer_suggestions(
    recipe_id: str,
    region: str,
    group: str,
    max_swaps: int,
    use_llm: bool,
) -> dict[str, Any]:
    """Suggest one-step replacements for a known consumer-group blocker."""

    context = fetch_recipe_consumer_context(recipe_id, group)
    if context is None:
        raise HTTPException(
            status_code=404,
            detail=f"Recipe '{recipe_id}' was not found in Neo4j.",
        )

    common = {
        "recipe_id": str(recipe_id),
        "region": region.upper(),
        "mode": group,
        "target_consumer_group": group,
        "current_consumer_status": context["status"],
        "blocking_ingredients": context["blocking_ingredients"],
        "unknown_ingredients": context["unknown_ingredients"],
    }
    if context["status"] == "suitable":
        return {
            **common,
            "status": "already_optimal",
            "message": f"Recipe is already compositionally suitable for {group}.",
            "suggestions": [],
        }

    blockers = list(context["blocking_ingredients"])
    if not blockers:
        return {
            **common,
            "status": "no_suggestions",
            "message": (
                f"No explicitly not-suitable {group} ingredient was found. "
                "Unknown ingredients require more evidence and are not "
                "automatically replaced."
            ),
            "suggestions": [],
        }

    # Vegan adaptation returns a complete recalculated recipe, so a regional
    # source profile is mandatory rather than optional.
    profile_row = _load_profile(recipe_id, region)
    nutrition_source = _profile_source(profile_row, region)
    profile_details = _recompute_ingredient_details(
        profile_row,
        nutrition_source,
    )
    graph_recipe = fetch_recipe_info_by_id(recipe_id)
    if not graph_recipe:
        raise HTTPException(
            status_code=404,
            detail=f"Recipe '{recipe_id}' was not found in Neo4j.",
        )

    existing = {
        str(item.get("name") or "").strip().casefold()
        for item in context["ingredients"]
        if item.get("name")
    }
    offender: str | None = None
    offender_detail: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    for blocking_ingredient in blockers:
        profiled_blocker = _find_profile_detail_for_graph_ingredient(
            profile_details,
            blocking_ingredient,
        )
        if profiled_blocker is None:
            continue
        candidate_pool = _consumer_candidate_pool(
            blocking_ingredient,
            group,
            existing,
            context["title"],
        )
        nutrition_backed_candidates: list[dict[str, Any]] = []
        for candidate in candidate_pool:
            candidate_profile = _fetch_candidate_profile(
                candidate["name"],
                nutrition_source,
            )
            if not candidate_profile:
                continue
            if not _nutrition_match_preserves_consumer_identity(
                candidate["name"],
                blocking_ingredient,
                candidate_profile,
                group,
            ):
                continue
            nutrition_backed = dict(candidate)
            nutrition_backed["_nutrition_profile"] = candidate_profile
            nutrition_backed_candidates.append(nutrition_backed)
        if nutrition_backed_candidates:
            offender = blocking_ingredient
            offender_detail = profiled_blocker
            candidates = nutrition_backed_candidates
            break

    if offender is None or offender_detail is None or not candidates:
        return {
            **common,
            "status": "no_suggestions",
            "message": (
                f"No substitute was both explicitly {group}-suitable and "
                f"reliably matched in the {region.upper()} nutrition data "
                "for the recipe's known blocking ingredients."
            ),
            "suggestions": [],
        }

    original_allergens = set(get_ingredient_allergens(offender))
    remaining_blockers = [
        ingredient for ingredient in blockers if ingredient != offender
    ]
    simulated_consumer_status = (
        "not_suitable"
        if remaining_blockers
        else "unknown"
        if context["unknown_ingredients"]
        else "suitable"
    )
    pool_size = max(max_swaps, 10) if use_llm else max(1, max_swaps)
    suggestions: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates[:pool_size], start=1):
        candidate_profile = candidate["_nutrition_profile"]
        candidate_allergens = set(
            get_ingredient_allergens(candidate["name"])
        )
        new_allergens = sorted(candidate_allergens - original_allergens)
        suggestions.append(
            {
                "rank": rank,
                "action": "swap",
                "original_ingredient": offender,
                "substitute_name": candidate["name"],
                "source": candidate["source"],
                "category_distance": candidate["category_distance"],
                "flavor_similarity": None,
                "introduces_allergen": bool(new_allergens),
                "new_allergens": new_allergens,
                "suitability_status": candidate["suitability_status"],
                "suitability_reasons": candidate[
                    "suitability_reasons"
                ],
                "suitability_classification_version": candidate[
                    "classification_version"
                ],
                "simulated_consumer_status": simulated_consumer_status,
                "nutrition_match": _nutrition_match_summary(
                    candidate,
                    candidate_profile,
                ),
                "adapted_recipe": _build_adapted_recipe_preview(
                    graph_recipe=graph_recipe,
                    profile_row=profile_row,
                    profile_details=profile_details,
                    offender_detail=offender_detail,
                    original_name=offender,
                    candidate_name=candidate["name"],
                    candidate_profile=candidate_profile,
                    region=region,
                    group=group,
                    simulated_consumer_status=simulated_consumer_status,
                ),
                "explanation": _consumer_explanation(
                    group,
                    offender,
                    candidate,
                    remaining_blockers,
                    context["unknown_ingredients"],
                    new_allergens,
                ),
                "llm_justification": None,
            }
        )

    llm_used = False
    llm_model = None
    llm_source = None
    llm_rejected: list[dict[str, Any]] = []
    final_suggestions = suggestions
    if use_llm and suggestions:
        judge_result = rerank_with_llm(
            recipe_title=context["title"],
            recipe_ingredients=context["ingredients"],
            target_nutrient_label=None,
            target_points=None,
            offending_ingredient=offender,
            offending_pct=0.0,
            candidates=suggestions,
            mode=group,
        )
        if judge_result:
            final_suggestions = judge_result["ranked"]
            llm_rejected = judge_result.get("rejected") or []
            llm_used = True
            llm_model = judge_result.get("model")
            llm_source = judge_result.get("source")

    final_suggestions = final_suggestions[: max(1, max_swaps)]
    for rank, suggestion in enumerate(final_suggestions, start=1):
        suggestion["rank"] = rank

    return {
        **common,
        "status": "ok",
        "offending_ingredient": offender,
        "suggestions": final_suggestions,
        "llm_used": llm_used,
        "llm_model": llm_model,
        "llm_source": llm_source,
        "llm_rejected": llm_rejected,
    }


# ---------- sustainability helpers ----------


# Minimum relative CF improvement required for a candidate to be considered.
# Filters out trivial swaps (e.g. switching between olive-oil varieties at 3.8 → 3.7).
SUSTAINABILITY_MIN_REDUCTION_PCT = 0.10


def _enrich_with_co2e(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mutate each detail in-place with sustainability fields and return the list.

    For each ingredient we look up its kg-CO2e-per-kg via the existing
    `best_sustainability_match()` and compute its absolute contribution
    (kg CO2e) from its weight.
    """

    for d in details:
        name = (d.get("ingredient") or "").strip()
        weight = float(d.get("weight_g") or 0.0)
        cf_val: float | None = None
        matched = None
        confidence = "none"
        if name and weight > 0:
            try:
                cf_val, matched, confidence = best_sustainability_match(name)
            except Exception:
                cf_val, matched, confidence = None, None, "none"
        d["cf_val"] = float(cf_val) if cf_val is not None else None
        d["matched_sustainability_ingredient"] = matched
        d["sustainability_match_confidence"] = confidence
        d["co2e_kg"] = (weight / 1000.0) * float(cf_val) if cf_val is not None else 0.0
    return details


def _rank_sustainability_offenders(
    details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank ingredients by CO2e contribution descending; keep only those with substitution paths."""

    total = sum(float(d.get("co2e_kg") or 0.0) for d in details)
    sorted_details = sorted(details, key=lambda d: float(d.get("co2e_kg") or 0.0), reverse=True)
    out: list[dict[str, Any]] = []
    for d in sorted_details:
        co2e = float(d.get("co2e_kg") or 0.0)
        if co2e <= 0:
            continue
        recipe_name = (d.get("ingredient") or "").strip()
        graph_name = d.get("graph_name")
        if not recipe_name or not graph_name or not has_any_substitution_path(graph_name):
            continue
        out.append({
            "name": recipe_name,
            "graph_name": graph_name,
            "weight_g": float(d.get("weight_g") or 0.0),
            "cf_val": float(d.get("cf_val") or 0.0),
            "co2e_kg": co2e,
            "contribution_pct": (co2e / total) if total > 0 else 0.0,
            "detail": d,
            "total_co2e_kg": total,
        })
    return out


def _evaluate_sustainability_candidate(
    candidate: dict[str, Any],
    offender: dict[str, Any],
    details: list[dict[str, Any]],
    serves: float,
    original_allergens: set[str],
    source: str,
    fvl_pct: float,
    current_grade: str,
) -> dict[str, Any] | None:
    """Look up CF for ``candidate``, filter, compute simulated CO2e, attach metadata.

    Nutri-guard: a candidate that cuts CO2e but worsens the recipe's Nutri-Score
    grade is rejected, so sustainability suggestions never silently sabotage the
    health axis.
    """

    # Food-class guard: reject cross-category swaps (e.g. beef→dried thyme) up front.
    if not _food_class_compatible(offender["name"], candidate["name"]):
        return None

    try:
        cand_cf, _matched, _conf = best_sustainability_match(candidate["name"])
    except Exception:
        cand_cf = None
    if cand_cf is None or cand_cf <= 0:
        return None
    cand_cf = float(cand_cf)

    orig_cf = float(offender["cf_val"])
    if orig_cf <= 0:
        return None
    reduction_pct = (orig_cf - cand_cf) / orig_cf
    if reduction_pct < SUSTAINABILITY_MIN_REDUCTION_PCT:
        return None

    # Nutri-guard: simulate the swap's nutrition and drop it if the grade worsens.
    # If the candidate has no composition match we can't judge nutrition — keep it
    # (the CO2e benefit is known; the LLM judge is a further backstop).
    cand_profile = _fetch_candidate_profile(candidate["name"], source)
    if cand_profile:
        _t, guard_per_100g, _w = _recipe_per_100g(
            details,
            swap_original_name=offender["name"],
            swap_weight_g=offender["weight_g"],
            swap_candidate_per_100g=_candidate_per_100g_map(cand_profile),
        )
        try:
            guard_breakdown = compute_nutri_score_breakdown_from_values(
                _ns_inputs_from_per_100g(guard_per_100g, fvl_pct), "solid"
            )
            if _grade_rank(_grade_letter(guard_breakdown.get("nutri_score"))) > _grade_rank(current_grade):
                return None
        except Exception:
            pass

    # Recompute total CO2e with the swap applied at the same weight.
    orig_lower = offender["name"].strip().lower()
    new_total_co2e_kg = 0.0
    for d in details:
        name = (d.get("ingredient") or "").strip().lower()
        weight = float(d.get("weight_g") or 0.0)
        if name == orig_lower:
            new_total_co2e_kg += (weight / 1000.0) * cand_cf
        else:
            new_total_co2e_kg += float(d.get("co2e_kg") or 0.0)

    total_old = offender["total_co2e_kg"]
    reduction_total_kg = total_old - new_total_co2e_kg
    if reduction_total_kg <= 0:
        return None
    reduction_per_serving_kg = reduction_total_kg / (serves or 1.0)

    cand_allergens = set(get_ingredient_allergens(candidate["name"]))
    new_allergens = sorted(cand_allergens - original_allergens)

    return {
        "candidate_name": candidate["name"],
        "source": candidate["source"],
        "category_distance": candidate["category_distance"],
        "candidate_cf": cand_cf,
        "original_cf": orig_cf,
        "reduction_pct": reduction_pct,            # of CF value (kg CO2e/kg)
        "new_total_co2e_kg": new_total_co2e_kg,
        "new_per_serving_co2e_kg": new_total_co2e_kg / (serves or 1.0),
        "reduction_per_serving_kg": reduction_per_serving_kg,
        "reduction_total_kg": reduction_total_kg,
        "introduces_allergen": bool(new_allergens),
        "new_allergens": new_allergens,
    }


def _build_co2e_explanation(
    original_name: str,
    candidate_name: str,
    original_cf: float,
    candidate_cf: float,
    reduction_per_serving_kg: float,
    new_allergens: list[str],
) -> dict[str, Any]:
    warning = None
    if new_allergens:
        warning = "Introduces allergen(s): " + ", ".join(new_allergens) + "."
    return {
        "headline": f"Swap {original_name} → {candidate_name}",
        "reason": (
            f"{original_name.capitalize()} has a carbon footprint of "
            f"{original_cf:.2f} kg CO2e/kg. {candidate_name.capitalize()} has "
            f"{candidate_cf:.2f} kg CO2e/kg, cutting the recipe's emissions by "
            f"about {reduction_per_serving_kg * 1000:.0f} g CO2e per serving."
        ),
        "warning": warning,
    }


def _generate_sustainability_suggestions(
    recipe_id: str, region: str, max_swaps: int, use_llm: bool,
) -> dict[str, Any]:
    """Sustainability-mode orchestrator: target the top CO2e contributor with substitutes."""

    row = _load_profile(recipe_id, region)
    source = _profile_source(row, region)
    details = _recompute_ingredient_details(row, source)
    details = _enrich_with_co2e(details)
    serves = _serves_from_row(row)

    # Context for the nutri-guard (a CO2e swap must not worsen the grade).
    breakdown = row.get("nutri_score_breakdown") or {}
    fvl_pct = _fvl_pct_from_breakdown(breakdown)
    current_grade = _authoritative_grade(recipe_id, breakdown)

    current_total_co2e_kg = sum(float(d.get("co2e_kg") or 0.0) for d in details)
    if current_total_co2e_kg <= 0:
        return _no_suggestions_response(
            recipe_id, region, "sustainability",
            "Could not compute a CO2e footprint — none of the ingredients matched "
            "the sustainability database.",
            breakdown,
        )
    current_per_serving_co2e_kg = current_total_co2e_kg / (serves or 1.0)

    offender_pool = _rank_sustainability_offenders(details)
    if not offender_pool:
        return _no_suggestions_response(
            recipe_id, region, "sustainability",
            "No CO2e-contributing ingredient has viable substitutes in the graph.",
            breakdown,
        )

    offender: dict[str, Any] | None = None
    evaluated: list[dict[str, Any]] = []
    for candidate_offender in offender_pool:
        raw_candidates = find_substitute_candidates(candidate_offender["graph_name"])
        if not raw_candidates:
            continue
        original_allergens = set(get_ingredient_allergens(candidate_offender["graph_name"]))
        results: list[dict[str, Any]] = []
        for cand in raw_candidates:
            result = _evaluate_sustainability_candidate(
                cand, candidate_offender, details, serves, original_allergens,
                source, fvl_pct, current_grade,
            )
            if result:
                results.append(result)
        if results:
            offender = candidate_offender
            evaluated = results
            break

    if not offender or not evaluated:
        return _no_suggestions_response(
            recipe_id, region, "sustainability",
            f"No substitute cuts CO2e by at least "
            f"{int(SUSTAINABILITY_MIN_REDUCTION_PCT * 100)}% for any ingredient.",
            breakdown,
        )

    # Rank by absolute CO2e reduction per serving — biggest climate impact first,
    # FlavorDB similarity to the original as the tiebreak.
    for e in evaluated:
        e["flavor_similarity"] = flavor_similarity(offender["graph_name"], e["candidate_name"])
    evaluated.sort(
        key=lambda e: (
            e["reduction_per_serving_kg"],
            e["flavor_similarity"] if e["flavor_similarity"] is not None else -1.0,
        ),
        reverse=True,
    )
    pool_size = max(max_swaps, 10) if use_llm else max(1, max_swaps)
    pool = evaluated[:pool_size]

    pool_suggestions: list[dict[str, Any]] = []
    for rank, e in enumerate(pool, start=1):
        explanation = _build_co2e_explanation(
            original_name=offender["name"],
            candidate_name=e["candidate_name"],
            original_cf=e["original_cf"],
            candidate_cf=e["candidate_cf"],
            reduction_per_serving_kg=e["reduction_per_serving_kg"],
            new_allergens=e["new_allergens"],
        )
        pool_suggestions.append({
            "rank": rank,
            "action": "swap",
            "original_ingredient": offender["name"],
            "substitute_name": e["candidate_name"],
            "source": e["source"],
            "category_distance": e["category_distance"],
            "flavor_similarity": e.get("flavor_similarity"),
            "introduces_allergen": e["introduces_allergen"],
            "new_allergens": e["new_allergens"],
            "explanation": explanation,
            "llm_justification": None,
            # Sustainability-specific fields:
            "simulated_co2e_per_serving_kg": e["new_per_serving_co2e_kg"],
            "co2e_reduction_per_serving_kg": e["reduction_per_serving_kg"],
            "co2e_reduction_pct": e["reduction_pct"],
            "original_cf_kg_co2e_per_kg": e["original_cf"],
            "candidate_cf_kg_co2e_per_kg": e["candidate_cf"],
        })

    # Optional LLM filter+rerank, fail-open.
    llm_used = False
    llm_model = None
    llm_source = None
    llm_rejected: list[dict[str, Any]] = []
    final_suggestions = pool_suggestions
    if use_llm and pool_suggestions:
        judge_result = rerank_with_llm(
            recipe_title=row.get("title") or "recipe",
            recipe_ingredients=details,
            target_nutrient_label=None,  # sustainability mode → judge picks up CO2e context
            target_points=None,
            offending_ingredient=offender["name"],
            offending_pct=round(offender["contribution_pct"] * 100.0, 1),
            candidates=pool_suggestions,
            mode="sustainability",
        )
        if judge_result:
            final_suggestions = judge_result["ranked"]
            llm_rejected = judge_result.get("rejected") or []
            llm_used = True
            llm_model = judge_result.get("model")
            llm_source = judge_result.get("source")

    final_suggestions = final_suggestions[: max(1, max_swaps)]
    for i, s in enumerate(final_suggestions, start=1):
        s["rank"] = i

    return {
        "recipe_id": str(recipe_id),
        "region": region.upper(),
        "mode": "sustainability",
        "offending_ingredient": offender["name"],
        "offending_ingredient_contribution_pct": round(offender["contribution_pct"] * 100.0, 1),
        "current_co2e_per_serving_kg": current_per_serving_co2e_kg,
        "current_co2e_total_kg": current_total_co2e_kg,
        "suggestions": final_suggestions,
        "llm_used": llm_used,
        "llm_model": llm_model,
        "llm_source": llm_source,
        "llm_rejected": llm_rejected,
    }


# ---------- reduce-quantity mode ----------


# Candidate retained fractions, smallest reduction first — we recommend the
# least cut that improves the grade. 0.3 (keep 30%) is the floor; cutting an
# ingredient further would usually wreck the dish.
REDUCE_KEEP_FRACTIONS = (0.7, 0.5, 0.3)


def _generate_reduce_quantity_suggestions(
    recipe_id: str, region: str, max_swaps: int,
) -> dict[str, Any]:
    """Reduce-quantity orchestrator: when no swap helps, recommend using less of
    the worst contributor to the target nutrient — the smallest reduction that
    improves the Nutri-Score grade."""

    row = _load_profile(recipe_id, region)
    breakdown = row["nutri_score_breakdown"]
    source = _profile_source(row, region)
    details = _recompute_ingredient_details(row, source)

    target = _identify_target_nutrient(breakdown)
    if not target:
        return _already_optimal_response(recipe_id, region, "reduce_quantity", breakdown)

    offender_pool = _rank_offender_candidates(details, target, require_substitutes=False)
    if not offender_pool:
        return _no_suggestions_response(
            recipe_id, region, "reduce_quantity",
            f"No ingredient contributes to {target['label']}.",
            breakdown,
        )

    fvl_pct = _fvl_pct_from_breakdown(breakdown)
    serves = _serves_from_row(row)
    current_grade = _authoritative_grade(recipe_id, breakdown)

    suggestions: list[dict[str, Any]] = []
    top_offender: dict[str, Any] | None = None
    for off in offender_pool:
        det = off["detail"]
        orig_w = float(off["weight_g"])
        if orig_w <= 0:
            continue
        # The offender's own per-100g profile — reducing weight just scales it down.
        own_per_100g = {p: float(det.get(p) or 0.0) for _a, p in ALL_INGREDIENT_KEYS}
        found = None
        for keep in REDUCE_KEEP_FRACTIONS:
            new_w = orig_w * keep
            _t, new_per_100g, _w = _recipe_per_100g(
                details,
                swap_original_name=off["name"],
                swap_weight_g=new_w,
                swap_candidate_per_100g=own_per_100g,
            )
            try:
                nb = compute_nutri_score_breakdown_from_values(
                    _ns_inputs_from_per_100g(new_per_100g, fvl_pct), "solid"
                )
            except Exception:
                continue
            sim_grade = _grade_letter(nb.get("nutri_score"))
            if _grade_rank(sim_grade) < _grade_rank(current_grade):
                new_target_points = (
                    ((nb.get("negative_points") or {}).get("items") or {})
                    .get(target["score_key"], {})
                    .get("points", target["points"])
                )
                found = {
                    "keep": keep,
                    "new_w": new_w,
                    "sim_grade": sim_grade,
                    "points_saved": int(target["points"]) - int(new_target_points),
                }
                break  # smallest reduction that works
        if not found:
            continue
        if top_offender is None:
            top_offender = off
        pct_removed = round((1 - found["keep"]) * 100)
        unit = target["unit"]
        fmt = "{:.1f}".format if unit != "kcal" else "{:.0f}".format
        explanation = {
            "headline": f"Use {pct_removed}% less {off['name']}",
            "reason": (
                f"{off['name'].capitalize()} is the biggest source of {target['label']} "
                f"in this recipe. Cutting it from {orig_w:.0f}g to {found['new_w']:.0f}g "
                f"improves the grade from {current_grade} to {found['sim_grade']} "
                f"(saves {found['points_saved']} Nutri-Score points). "
                f"The recipe still has {fmt(off['original_per_100g'])}{unit} of "
                f"{target['label']} per 100g in the kept portion."
            ),
            "warning": None,
        }
        suggestions.append({
            "rank": len(suggestions) + 1,
            "action": "reduce",
            "original_ingredient": off["name"],
            "substitute_name": None,
            "source": None,
            "category_distance": None,
            "flavor_similarity": None,
            "simulated_nutri_score": found["sim_grade"],
            "nutri_score_points_saved": found["points_saved"],
            "reduced_from_weight_g": orig_w,
            "reduced_to_weight_g": found["new_w"],
            "reduction_pct": 1 - found["keep"],
            "introduces_allergen": False,
            "new_allergens": [],
            "explanation": explanation,
            "llm_justification": None,
        })
        if len(suggestions) >= max(1, max_swaps):
            break

    if not suggestions:
        return _no_suggestions_response(
            recipe_id, region, "reduce_quantity",
            f"No single-ingredient reduction (down to {int(REDUCE_KEEP_FRACTIONS[-1] * 100)}% "
            "of original weight) improves the grade.",
            breakdown,
        )

    return {
        "recipe_id": str(recipe_id),
        "region": region.upper(),
        "mode": "reduce_quantity",
        "current_nutri_score": current_grade,
        "target_nutrient": target["ns_input_key"],
        "target_nutrient_label": target["label"],
        "target_nutrient_points": target["points"],
        "target_nutrient_points_max": NUTRI_SCORE_MAX_NEGATIVE_POINTS,
        "offending_ingredient": top_offender["name"],
        "offending_ingredient_contribution_pct": round(top_offender["contribution_pct"] * 100.0, 1),
        "suggestions": suggestions,
        "llm_used": False,
        "llm_model": None,
        "llm_source": None,
        "llm_rejected": [],
    }


# ---------- public entry points ----------


def _generate_portion_suggestion(
    recipe_id: str, region: str, target_serves: float | None,
) -> dict[str, Any]:
    if target_serves is None:
        raise HTTPException(status_code=422, detail="target_serves is required for portion mode")
    row = _load_profile(recipe_id, region)
    source = _region_to_source(region)
    details = _recompute_ingredient_details(row, source)
    current_serves = _serves_from_row(row)
    factor = float(target_serves) / current_serves
    scaled = [
        {
            "name": item.get("ingredient"),
            "weight_g": round(float(item.get("weight_g") or 0.0) * factor, 1),
        }
        for item in details
    ]
    return {
        "recipe_id": recipe_id,
        "region": region.upper(),
        "mode": "portion",
        "offending_ingredient": "",
        "offending_ingredient_contribution_pct": 0.0,
        "suggestions": [{
            "rank": 1,
            "action": "scale",
            "original_ingredient": "whole recipe",
            "explanation": {
                "headline": f"Scale recipe to {target_serves:g} servings",
                "reason": f"All profiled ingredient weights are multiplied by {factor:.3f}.",
                "warning": "Cooking vessel size and cooking time may still need manual adjustment.",
            },
            "adjusted_serves": target_serves,
            "scale_factor": round(factor, 4),
            "adapted_recipe": {
                "title": row.get("title"),
                "serves": target_serves,
                "ingredients": scaled,
            },
        }],
        "llm_used": False,
        "llm_rejected": [],
    }


def _already_optimal_response(
    recipe_id: str, region: str, mode: str, breakdown: dict[str, Any],
) -> dict[str, Any]:
    """A recipe with no nutrient scoring >= MIN_TARGET_POINTS needs no
    adaptation — that is a successful outcome, not an error."""
    return {
        "recipe_id": recipe_id,
        "region": region,
        "mode": mode,
        "status": "already_optimal",
        "message": (
            f"Recipe already scores below {MIN_TARGET_POINTS} on every negative "
            "Nutri-Score nutrient — no adaptation needed."
        ),
        "current_nutri_score": _authoritative_grade(recipe_id, breakdown),
        "suggestions": [],
    }


def _no_suggestions_response(
    recipe_id: str, region: str, mode: str, message: str,
    breakdown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A target exists but no viable swap/reduction was found — a legitimate
    analysis outcome the UI renders as an empty state, not an error."""
    payload: dict[str, Any] = {
        "recipe_id": recipe_id,
        "region": region,
        "mode": mode,
        "status": "no_suggestions",
        "message": message,
        "suggestions": [],
    }
    if isinstance(breakdown, dict):
        payload["current_nutri_score"] = _authoritative_grade(recipe_id, breakdown)
    return payload


def generate_suggestions(
    recipe_id: str, region: str, max_swaps: int = 1, use_llm: bool = False,
    mode: str = "nutrition", goal_nutrients: list[str] | None = None,
    target_serves: float | None = None,
) -> dict[str, Any]:
    mode_l = (mode or "").lower()
    if mode_l in {"vegan", "vegetarian"}:
        return _generate_consumer_suggestions(
            recipe_id=recipe_id,
            region=region,
            group=mode_l,
            max_swaps=max_swaps,
            use_llm=use_llm,
        )
    if mode_l == "sustainability":
        return _generate_sustainability_suggestions(
            recipe_id=recipe_id, region=region, max_swaps=max_swaps, use_llm=use_llm,
        )
    if mode_l == "reduce_quantity":
        return _generate_reduce_quantity_suggestions(
            recipe_id=recipe_id, region=region, max_swaps=max_swaps,
        )
    if mode_l == "portion":
        return _generate_portion_suggestion(recipe_id, region, target_serves)

    row = _load_profile(recipe_id, region)
    breakdown = row["nutri_score_breakdown"]
    source = _profile_source(row, region)
    details = _recompute_ingredient_details(row, source)

    target = _identify_target_nutrient(breakdown, _normalize_goal_nutrients(goal_nutrients))
    if not target:
        return _already_optimal_response(recipe_id, region, "nutrition", breakdown)

    offender_pool = _rank_offender_candidates(details, target)
    if not offender_pool:
        return _no_suggestions_response(
            recipe_id, region, "nutrition",
            f"No ingredient with viable substitutes contributes to {target['label']}.",
            breakdown,
        )

    fvl_pct = _fvl_pct_from_breakdown(breakdown)
    serves = _serves_from_row(row)
    current_grade = _authoritative_grade(recipe_id, breakdown)

    # Walk down the offender list until we hit one that yields ≥1 viable suggestion.
    offender: dict[str, Any] | None = None
    evaluated: list[dict[str, Any]] = []
    for candidate_offender in offender_pool:
        raw_candidates = find_substitute_candidates(candidate_offender["graph_name"])
        if not raw_candidates:
            continue
        original_allergens = set(get_ingredient_allergens(candidate_offender["graph_name"]))
        results: list[dict[str, Any]] = []
        for cand in raw_candidates:
            result = _evaluate_candidate(
                cand,
                candidate_offender,
                target,
                details,
                fvl_pct,
                breakdown,
                source,
                original_allergens,
                serves,
                current_grade=current_grade,
            )
            if result:
                results.append(result)
        if results:
            offender = candidate_offender
            evaluated = results
            break

    if not offender or not evaluated:
        return _no_suggestions_response(
            recipe_id, region, "nutrition",
            f"No viable substitute improves {target['label']} for any ingredient in this recipe.",
            breakdown,
        )

    # FlavorDB tiebreak: among candidates that save equal points / improve
    # equally, prefer the one whose flavor profile is closest to the original.
    for e in evaluated:
        e["flavor_similarity"] = flavor_similarity(offender["graph_name"], e["candidate_name"])
    evaluated.sort(
        key=lambda e: (
            e["points_saved"],
            e["relative_improvement"],
            e["flavor_similarity"] if e["flavor_similarity"] is not None else -1.0,
        ),
        reverse=True,
    )

    # When the LLM judge is on, hand it a deeper pool (up to 10) so it has real
    # choice. Without LLM, top max_swaps is enough.
    pool_size = max(max_swaps, 10) if use_llm else max(1, max_swaps)
    candidate_pool = evaluated[:pool_size]

    # Materialise deterministic suggestion dicts for every candidate in the pool.
    pool_suggestions: list[dict[str, Any]] = []
    for rank, e in enumerate(candidate_pool, start=1):
        simulated_grade = _grade_letter(e["new_breakdown"].get("nutri_score"))
        explanation = _build_explanation(
            target_label=target["label"],
            target_points=target["points"],
            original_name=offender["name"],
            original_contribution_g=offender["contribution"],
            serves=serves,
            candidate_name=e["candidate_name"],
            candidate_per_100g=e["candidate_per_100g_target"],
            original_per_100g=e["original_per_100g_target"],
            points_saved=e["points_saved"],
            current_grade=current_grade,
            simulated_grade=simulated_grade,
            new_allergens=e["new_allergens"],
            unit=target["unit"],
        )
        pool_suggestions.append({
            "rank": rank,
            "action": "swap",
            "original_ingredient": offender["name"],
            "substitute_name": e["candidate_name"],
            "source": e["source"],
            "category_distance": e["category_distance"],
            "flavor_similarity": e.get("flavor_similarity"),
            "simulated_nutri_score": simulated_grade,
            "nutri_score_points_saved": e["points_saved"],
            "relative_improvement": e["relative_improvement"],
            "target_nutrient_per_100g": e["candidate_per_100g_target"],
            "original_per_100g": e["original_per_100g_target"],
            "nutrient_delta_per_serving": e["delta_per_serving"],
            "introduces_allergen": e["introduces_allergen"],
            "new_allergens": e["new_allergens"],
            "explanation": explanation,
            "llm_justification": None,
        })

    # Optional LLM filter+rerank. Always fails open to the deterministic pool.
    llm_used = False
    llm_model: str | None = None
    llm_source: str | None = None
    llm_rejected: list[dict[str, Any]] = []
    final_suggestions = pool_suggestions
    if use_llm and pool_suggestions:
        judge_result = rerank_with_llm(
            recipe_title=row.get("title") or "recipe",
            recipe_ingredients=details,
            target_nutrient_label=target["label"],
            target_points=target["points"],
            offending_ingredient=offender["name"],
            offending_pct=round(offender["contribution_pct"] * 100.0, 1),
            candidates=pool_suggestions,
        )
        if judge_result:
            final_suggestions = judge_result["ranked"]
            llm_rejected = judge_result.get("rejected") or []
            llm_used = True
            llm_model = judge_result.get("model")
            llm_source = judge_result.get("source")

    # Final truncation + renumber.
    final_suggestions = final_suggestions[: max(1, max_swaps)]
    for i, s in enumerate(final_suggestions, start=1):
        s["rank"] = i

    return {
        "recipe_id": str(recipe_id),
        "region": region.upper(),
        "mode": "nutrition",
        "current_nutri_score": current_grade,
        "target_nutrient": target["ns_input_key"],
        "target_nutrient_label": target["label"],
        "target_nutrient_points": target["points"],
        "target_nutrient_points_max": NUTRI_SCORE_MAX_NEGATIVE_POINTS,
        "offending_ingredient": offender["name"],
        "offending_ingredient_contribution_pct": round(offender["contribution_pct"] * 100.0, 1),
        "suggestions": final_suggestions,
        "llm_used": llm_used,
        "llm_model": llm_model,
        "llm_source": llm_source,
        "llm_rejected": llm_rejected,
    }


def simulate_swap(
    recipe_id: str,
    region: str,
    original_ingredient: str,
    substitute_ingredient: str,
    weight_g: float | None = None,
) -> dict[str, Any]:
    row = _load_profile(recipe_id, region)
    breakdown = row["nutri_score_breakdown"]
    source = _profile_source(row, region)
    details = _recompute_ingredient_details(row, source)

    # Locate original ingredient in the profile.
    original_lower = original_ingredient.strip().lower()
    original_detail = next(
        (d for d in details if (d.get("ingredient") or "").strip().lower() == original_lower),
        None,
    )
    if not original_detail:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Original ingredient '{original_ingredient}' not found in profile for "
                f"recipe '{recipe_id}'."
            ),
        )

    candidate_profile = _fetch_candidate_profile(substitute_ingredient, source)
    if not candidate_profile:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not match substitute '{substitute_ingredient}' in the "
                f"{source} composition table."
            ),
        )
    cand_per_100g = _candidate_per_100g_map(candidate_profile)

    effective_weight = float(weight_g) if weight_g else float(original_detail.get("weight_g") or 0.0)

    orig_totals, orig_per_100g, _ = _recipe_per_100g(details)
    new_totals, new_per_100g, _ = _recipe_per_100g(
        details,
        swap_original_name=original_ingredient,
        swap_weight_g=effective_weight,
        swap_candidate_per_100g=cand_per_100g,
    )

    fvl_pct = _fvl_pct_from_breakdown(breakdown)
    new_breakdown = compute_nutri_score_breakdown_from_values(
        _ns_inputs_from_per_100g(new_per_100g, fvl_pct), "solid"
    )

    original_grade = _grade_letter(breakdown.get("nutri_score"))
    simulated_grade = _grade_letter(new_breakdown.get("nutri_score"))
    original_score = int(breakdown.get("score") or 0)
    simulated_score = int(new_breakdown.get("score") or 0)

    serves = _serves_from_row(row)
    divisor = serves if serves > 0 else 1.0
    orig_ps = {k: v / divisor for k, v in orig_totals.items()}
    new_ps = {k: v / divisor for k, v in new_totals.items()}
    delta_per_100g = {k: new_per_100g[k] - orig_per_100g[k] for k in orig_per_100g}
    delta_per_serving = {k: new_ps[k] - orig_ps[k] for k in orig_ps}

    # CO2e impact of the swap — informational; surfaced regardless of mode.
    orig_cf = None
    cand_cf = None
    try:
        orig_cf_val, _, _ = best_sustainability_match(original_ingredient)
        orig_cf = float(orig_cf_val) if orig_cf_val is not None else None
    except Exception:
        orig_cf = None
    try:
        cand_cf_val, _, _ = best_sustainability_match(substitute_ingredient)
        cand_cf = float(cand_cf_val) if cand_cf_val is not None else None
    except Exception:
        cand_cf = None

    original_co2e_per_serving_kg = None
    simulated_co2e_per_serving_kg = None
    co2e_reduction_per_serving_kg = None
    if orig_cf is not None or cand_cf is not None:
        original_weight_g = float(original_detail.get("weight_g") or 0.0)
        # Build current total CO2e across all ingredients (best_sustainability_match per ingredient).
        total_co2e_kg = 0.0
        for d in details:
            try:
                cf, _, _ = best_sustainability_match((d.get("ingredient") or "").strip())
            except Exception:
                cf = None
            if cf is not None:
                total_co2e_kg += (float(d.get("weight_g") or 0.0) / 1000.0) * float(cf)
        # Substitute the offender's CO2e contribution.
        orig_contrib = (original_weight_g / 1000.0) * float(orig_cf or 0.0)
        cand_contrib = (effective_weight / 1000.0) * float(cand_cf or 0.0)
        new_total_co2e_kg = total_co2e_kg - orig_contrib + cand_contrib
        original_co2e_per_serving_kg = total_co2e_kg / divisor
        simulated_co2e_per_serving_kg = new_total_co2e_kg / divisor
        co2e_reduction_per_serving_kg = original_co2e_per_serving_kg - simulated_co2e_per_serving_kg

    return {
        "recipe_id": str(recipe_id),
        "region": region.upper(),
        "original_nutri_score": original_grade,
        "simulated_nutri_score": simulated_grade,
        "nutri_score_points_delta": simulated_score - original_score,
        "original_total_nutrients_per_100g": orig_per_100g,
        "simulated_total_nutrients_per_100g": new_per_100g,
        "original_total_nutrients_per_serving": orig_ps,
        "simulated_total_nutrients_per_serving": new_ps,
        "nutrient_delta": {
            "per_100g": delta_per_100g,
            "per_serving": delta_per_serving,
        },
        "simulated_nutri_score_breakdown": new_breakdown,
        "original_co2e_per_serving_kg": original_co2e_per_serving_kg,
        "simulated_co2e_per_serving_kg": simulated_co2e_per_serving_kg,
        "co2e_reduction_per_serving_kg": co2e_reduction_per_serving_kg,
    }
