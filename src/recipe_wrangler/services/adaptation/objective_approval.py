"""Production nutrition acceptance gates on explicitly comparable raw inputs."""

import math
from typing import Annotated, Literal

from pydantic import Field

from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values

from .comparison import compare_raw_part
from .models import (
    ComparisonConstraints, ComparisonModel, ComparisonResult, ComparisonSnapshot, Evidence, PositiveNumber, Text,
)
from .models import NutrientObjective
from .quantity_declarations import apply_quantity_declarations
from .impact_objectives import objective_delta
from .comparison import nutrition_basis
from .nutrition_benefit import NutritionPolicy, DEFAULT_POLICY, STRICT_POLICY, ROUNDING_TOLERANCE, assess_nutrition_benefit, validate_nutrition_policy


class InputScoreContext(ComparisonModel):
    """Caller-verified scoring assumptions; never inferred by the LLM.

    This supports an estimate per 100g raw edible inputs, not a finished-dish
    Nutri-Score. The caller must authenticate the evidence source.
    """

    recipe_id: Text
    baseline_run_id: Text
    quantity_input_hash: Text
    basis: Literal["raw_edible_input", "selected_state_input_estimate"] = "raw_edible_input"
    total_weight_g: PositiveNumber
    food_type: Literal["solid", "beverage"]
    fruit_veg_legume_percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, strict=True)]
    evidence: Evidence


def supported_score_context(context: InputScoreContext) -> bool:
    return context.evidence.status == "stated" or (
        context.evidence.status == "proxy" and context.evidence.method == "validated_production_score_context")


def _fvl_member(composition) -> bool | None:
    explicit = composition.properties.get("nutri_score_fvl_member")
    if explicit is not None and explicit.evidence.status == "stated" and isinstance(explicit.value, bool):
        return explicit.value
    identity = composition.properties.get("base_identity")
    if identity is None or identity.evidence.status != "stated":
        return None
    from recipe_wrangler.tools.nutrition_match import food_class
    from recipe_wrangler.utils.fruit_vegetable_content import is_fruit_vegetable_legume_or_nut
    family = food_class(identity.value)
    if family in {"animal_protein", "dairy", "egg", "grain_cereal", "sweetener",
                  "alcohol", "plant_milk", "condiment_sauce", "spice_herb"}:
        return False
    if family in {"fruit", "vegetable", "leafy_green", "legume", "nut_seed"}:
        return True if is_fruit_vegetable_legume_or_nut({"name": identity.value}) else None
    if family == "oil_fat":
        return is_fruit_vegetable_legume_or_nut({"name": identity.value})
    if identity.value in {"beef", "pork", "chicken", "lamb", "turkey", "fish", "milk", "egg", "eggs", "salt", "sugar", "water"}:
        return False
    if identity.value in {"apple", "pear", "carrot", "potato", "tomato", "onion", "broccoli", "spinach", "chickpea", "lentil", "bean", "almond", "walnut", "olive oil"}:
        return True
    return None


def production_score_context(snapshot: ComparisonSnapshot, profile: dict) -> InputScoreContext | None:
    """Reuse production's score assumptions after checking the same raw inputs.

    Production's food type and FVL classification are estimates, not new observed
    facts. Retain that proxy provenance; never manufacture a missing breakdown.
    """
    breakdown = profile.get("nutri_score_breakdown")
    if not isinstance(breakdown, dict) or breakdown.get("food_type") not in {"solid", "beverage"}:
        return None
    if profile.get("quantity_input_hash") not in (None, snapshot.quantity_input_hash):
        return None
    mass = sum(item.quantity.weight_g for item in snapshot.ingredients)
    if mass <= 0 or not math.isfinite(mass):
        return None
    try:
        saved_mass = breakdown["inputs"]["total_weight_g"]
        if isinstance(saved_mass, bool) or not math.isclose(saved_mass, mass, rel_tol=1e-9, abs_tol=1e-9):
            return None
        totals = profile["clean_totals"]
        for metric, section, key, factor in (
            ("energy_kcal", "negative_points", "energy", 4.184),
            ("sugar_g", "negative_points", "sugar", 1),
            ("saturated_fat_g", "negative_points", "saturated_fats", 1),
            ("sodium_mg", "negative_points", "sodium", 1),
            ("fibre_g", "positive_points", "fiber", 1),
            ("protein_g", "positive_points", "protein", 1),
        ):
            value = totals[metric]
            saved = breakdown[section]["items"][key]["value_per_100g"]
            if (isinstance(value, bool) or isinstance(saved, bool) or value < 0 or not math.isfinite(value)
                    or not math.isfinite(saved) or not math.isclose(value / mass * 100 * factor, saved, rel_tol=1e-7, abs_tol=1e-7)):
                return None
        fvl = breakdown["positive_points"]["items"]["fruit_percentage"]["value_per_100g"]
        if isinstance(fvl, bool) or not math.isfinite(fvl) or not 0 <= fvl <= 100:
            return None
        return InputScoreContext(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
            basis=nutrition_basis(snapshot),
            quantity_input_hash=snapshot.quantity_input_hash, total_weight_g=mass,
            food_type=breakdown["food_type"], fruit_veg_legume_percent=float(fvl),
            evidence=Evidence(reference="matching_profile_runs/" + snapshot.run_id + "/nutri_score_breakdown",
                method="validated_production_score_context", version="1", status="proxy"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def approve_nutrition_candidate(
    snapshot: ComparisonSnapshot, comparison: ComparisonResult, objective: NutrientObjective,
    score_context: InputScoreContext, *, constraints: ComparisonConstraints | None = None,
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
) -> dict:
    """Recompute both sides; require meaningful benefit and nonregressing nutrition."""
    validate_nutrition_policy(nutrition_policy)
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    comparison = ComparisonResult.model_validate(comparison.model_dump())
    objective = NutrientObjective.model_validate(objective.model_dump())
    score_context = InputScoreContext.model_validate(score_context.model_dump())
    result = {"version": "nutrition_objective_approval_v2", "status": "needs_evidence", "reasons": [],
        "nutrient_guard_policy": "conservative_no_regression_v1", "nutrient_deltas_per_serving": {},
        "basis": nutrition_basis(snapshot), "finished_recipe_score": False,
        "before": None, "after": None, "target_points_saved": None,
        "score_context": score_context.model_dump(mode="json")}
    keys = {"energy_kcal": "energy", "sugar_g": "sugar", "saturated_fat_g": "saturated_fats", "sodium_mg": "sodium"}
    impact = objective.metric in {"cost_eur", "co2e_kg"}
    if not impact and nutrition_policy != STRICT_POLICY:
        result["version"] = "nutrition_objective_approval_" + nutrition_policy
    if objective.direction != "decrease" or (objective.metric not in keys and not impact):
        result.update(status="blocked", reasons=["unsupported_production_nutrition_objective"])
        return result
    if (score_context.recipe_id, score_context.baseline_run_id, score_context.quantity_input_hash) != (
        snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash):
        result.update(status="blocked", reasons=["score_context_baseline_mismatch"])
        return result
    if not supported_score_context(score_context):
        result["reasons"] = ["stated_score_context_required"]
        return result
    if score_context.basis != nutrition_basis(snapshot):
        result.update(status="blocked", reasons=["score_context_basis_mismatch"])
        return result
    if comparison.status != "supported_input_comparison" or comparison.target_composition is None:
        result["reasons"] = ["supported_comparison_required"]
        return result
    if comparison.edit.nutritional_change is not None and comparison.edit.nutritional_change.metric != objective.metric:
        result.update(status="blocked", reasons=["nutritional_variant_objective_mismatch"])
        return result
    checked = compare_raw_part(snapshot, comparison.edit, comparison.target_composition, constraints,
        quantity_declarations=comparison.quantity_declarations)
    if checked.model_dump() != comparison.model_dump():
        result.update(status="blocked", reasons=["comparison_changed"])
        return result
    effective, _ = apply_quantity_declarations(snapshot, checked.quantity_declarations)
    mass = sum(item.quantity.weight_g for item in effective.ingredients)
    if mass <= 0 or not math.isfinite(mass) or not math.isclose(mass, score_context.total_weight_g, rel_tol=1e-9, abs_tol=1e-9):
        result.update(status="blocked", reasons=["score_context_mass_mismatch"])
        return result
    mapping = {"energy_kcal": ("energy", 4.184), "sugar_g": ("sugar", 1),
        "saturated_fat_g": ("saturated_fats", 1), "sodium_mg": ("sodium", 1),
        "fibre_g": ("fibers", 1), "protein_g": ("proteins", 1)}
    after_fvl = score_context.fruit_veg_legume_percent
    if checked.edit.source_identity != checked.edit.target_identity:
        source_member, target_member = _fvl_member(checked.source_composition), _fvl_member(checked.target_composition)
        if source_member is None or target_member is None:
            result["reasons"] = ["substitution_fvl_membership_evidence_required"]
            return result
        share = checked.quantity.weight_g / mass * 100
        if ((source_member and after_fvl + 1e-7 < share) or
                (not source_member and after_fvl > 100 - share + 1e-7)):
            result.update(status="blocked", reasons=["score_context_fvl_conflicts_with_source"])
            return result
        after_fvl += (int(target_member) - int(source_member)) * share
        if not -1e-7 <= after_fvl <= 100 + 1e-7:
            result.update(status="blocked", reasons=["invalid_substitution_fvl_percentage"])
            return result
        after_fvl = min(100., max(0., after_fvl))
    for side in ("before", "after"):
        totals = getattr(checked, side)["complete_totals"]
        if any(totals.get(key) is None for key in mapping):
            result["reasons"] = ["complete_comparable_score_nutrients_required"]
            return result
        values = {label: totals[key] / mass * 100 * factor for key, (label, factor) in mapping.items()}
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            result.update(status="blocked", reasons=["invalid_score_inputs"])
            return result
        values["fruit_percentage"] = score_context.fruit_veg_legume_percent if side == "before" else after_fvl
        result[side] = compute_nutri_score_breakdown_from_values(values, score_context.food_type)
    if not impact:
        guard_metrics = mapping if nutrition_policy in {STRICT_POLICY, DEFAULT_POLICY} else ROUNDING_TOLERANCE
        if nutrition_policy == DEFAULT_POLICY and any('incompatible_nutrient_definition' in checked.nutrient_blockers.get(metric, [])
                for metric in ('carbs_g', 'fat_g')):
            result['reasons'] = ['complete_comparable_guard_nutrients_required']
            return result
        if any(checked.recipe_delta_per_serving.get(metric) is None for metric in guard_metrics):
            result['reasons'] = ['complete_comparable_guard_nutrients_required']
            return result
        benefit = assess_nutrition_benefit(checked.before["complete_totals_per_serving"],
            checked.after["complete_totals_per_serving"], objective.metric, result["before"], result["after"],
            policy=nutrition_policy)
        result.update(benefit)
        if nutrition_policy == DEFAULT_POLICY:
            for metric in ('carbs_g', 'fat_g'):
                result['nutrient_deltas_per_serving'][metric] = checked.recipe_delta_per_serving.get(metric)
            result['unassessed_nutrients'] = [metric for metric in ('carbs_g', 'fat_g')
                if checked.recipe_delta_per_serving.get(metric) is None]
        return result
    # Until development cases justify trade-offs, an aggregate grade cannot excuse a nutrient regression.
    for metric in mapping:
        change = checked.recipe_delta_per_serving.get(metric)
        if change is None or not math.isfinite(change):
            result["reasons"] = ["complete_comparable_guard_nutrients_required"]
            return result
        result["nutrient_deltas_per_serving"][metric] = change
        before = checked.before["complete_totals_per_serving"][metric]
        after = checked.after["complete_totals_per_serving"][metric]
        worsened = after < before if metric in {"fibre_g", "protein_g"} else after > before
        if worsened and not math.isclose(before, after, rel_tol=1e-9, abs_tol=1e-9):
            result["reasons"].append("nutrient_regression:" + metric)
    delta = objective_delta(checked, objective)
    if delta is None or not math.isfinite(delta):
        result["reasons"] = ["complete_objective_per_serving_required"]
        return result
    if delta >= 0:
        result["reasons"].append("no_objective_improvement")
    grades = {"Nutriscore_" + letter: rank for rank, letter in enumerate("ABCDE")}
    before_grade, after_grade = result["before"]["nutri_score"], result["after"]["nutri_score"]
    if before_grade not in grades or after_grade not in grades:
        result.update(status="blocked", reasons=["invalid_score_grade"])
        return result
    if grades[after_grade] > grades[before_grade]:
        result["reasons"].append("nutrition_grade_regression")
    result["impact_estimate"] = {"metric": objective.metric, "delta_per_serving": delta,
        "before_total": objective.impact_context.baseline_total,
        "after_total": max(0., objective.impact_context.baseline_total + delta * checked.serves),
        "context": objective.impact_context.model_dump(mode="json"), "evidence_status": "proxy"}
    result["status"] = "rejected" if result["reasons"] else "approved_input_objective"
    return result
