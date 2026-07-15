"""Recipe adaptation service.

Reuses existing helpers (no modifications to upstream code):
  - `fetch_recipe_profiling_trace_by_id` from `utils.nutrition_postgres`
  - `nutritional_tool_chroma` from `tools.nutritional_calculator`
  - `compute_nutri_score_breakdown_from_values` from `utils.nutri_score`
  - Neo4j helpers in this package's `neo4j_queries`
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import HTTPException

from recipe_wrangler.tools.nutrition_match import food_class
from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_chroma
from recipe_wrangler.tools.sustainability_calculator import best_sustainability_match
from recipe_wrangler.utils.nutri_score import (
    compute_nutri_score_breakdown_from_values,
)
from recipe_wrangler.utils.nutrition_postgres import (
    fetch_recipe_profiling_trace_by_id,
)

from .llm_judge import rerank_with_llm
from .neo4j_queries import (
    fetch_recipe_default_nutriscore,
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


REGION_TO_SOURCE = {"IE": "irish", "US": "usda", "HU": "hungarian"}

MIN_TARGET_POINTS = 3
NUTRI_SCORE_MAX_NEGATIVE_POINTS = 10
CANDIDATE_MIN_SIMILARITY = 0.7

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

    result = nutritional_tool_chroma.invoke({
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
            detail="nutritional_tool_chroma returned a mismatched detail count.",
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
    butter, yoghurt, ...), and each lookup costs several Chroma/Postgres/USDA
    round-trips — the dominant share of a suggestions call's latency.
    """
    det = _fetch_candidate_profile_cached(
        str(candidate_name or "").strip().lower(), str(source or "")
    )
    return dict(det) if det is not None else None


@lru_cache(maxsize=1024)
def _fetch_candidate_profile_cached(candidate_name: str, source: str) -> dict[str, Any] | None:
    try:
        result = nutritional_tool_chroma.invoke({
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
    """Extract the per-100g values from a nutritional_tool_chroma detail row."""

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
    source = _region_to_source(region)
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
    source = _region_to_source(region)
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
) -> dict[str, Any]:
    mode_l = (mode or "").lower()
    if mode_l == "sustainability":
        return _generate_sustainability_suggestions(
            recipe_id=recipe_id, region=region, max_swaps=max_swaps, use_llm=use_llm,
        )
    if mode_l == "reduce_quantity":
        return _generate_reduce_quantity_suggestions(
            recipe_id=recipe_id, region=region, max_swaps=max_swaps,
        )

    row = _load_profile(recipe_id, region)
    breakdown = row["nutri_score_breakdown"]
    source = _region_to_source(region)
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
    source = _region_to_source(region)
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
