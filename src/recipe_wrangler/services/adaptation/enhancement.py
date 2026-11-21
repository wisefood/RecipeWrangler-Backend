"""Choose enhancement targets without inference, recipe edits or persistence.

This is the deterministic first stage of the one-click workflow. A plan is
not approval to execute a graph substitution or a claim of improved score.
"""

from collections.abc import Sequence
import math
from typing import Literal

from .models import ComparisonSnapshot, NutritionSource, QuantityBasisDeclaration
from .quantity_declarations import apply_quantity_declarations
from .comparison import estimated_inputs, nutrition_basis, supported_quantity, supported_reporting_basis
from .nutrition_benefit import NutritionPolicy, DEFAULT_POLICY, STRICT_POLICY, WITHIN_GRADE_POLICY, validate_nutrition_policy


EnhancementMode = Literal["nutrition", "sustainability", "cost"]


MAX_ENHANCEMENT_TARGETS = 10

def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def plan_enhancement(
    snapshot: ComparisonSnapshot, *, mode: EnhancementMode,
    nutri_score_breakdown: dict | None = None, goal_nutrients: list[str] | None = None,
    cost_country: str | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
) -> dict:
    """Rank occurrences using production objectives, retaining unknown coverage.

    The caller must pin the breakdown to this snapshot's active run. Catalogue
    price/emissions matches are proxies for target discovery, never verified
    replacement benefits. Unknown quantities cannot enter this raw-input plan.
    """
    validate_nutrition_policy(nutrition_policy)
    if mode not in {"nutrition", "sustainability", "cost"}:
        raise ValueError("Unsupported enhancement mode")
    # Keep production thresholds and member-goal preferences in one place.
    from .service import (
        COST_MIN_REDUCTION_PCT, SUSTAINABILITY_MIN_REDUCTION_PCT,
        _identify_target_nutrient, _normalize_goal_nutrients,
    )

    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    effective, _ = apply_quantity_declarations(snapshot, quantity_declarations)
    result = {
        "version": "enhancement_target_plan_v1", "mode": mode,
        "recipe_id": snapshot.recipe_id, "baseline_run_id": snapshot.run_id,
        "quantity_input_hash": snapshot.quantity_input_hash,
        "status": "needs_evidence", "reasons": [], "objective": None,
        "ranked_targets": [], "unknown_contributions": [],
        "supported_partial_total": 0.0, "complete_total": None,
        "execution_status": "not_executed", "adapted_recipe": None,
        "quantity_basis": nutrition_basis(snapshot), "recipe_ready": False, "saved": False,
    }
    target = None
    catalogue = None
    if mode == "nutrition":
        if not nutri_score_breakdown:
            result["reasons"] = ["active_nutri_score_breakdown_missing"]
            return result
        preferred = _normalize_goal_nutrients(goal_nutrients)
        if nutrition_policy != STRICT_POLICY:
            if not preferred and (goal_nutrients or nutrition_policy == WITHIN_GRADE_POLICY):
                result["reasons"] = ["within_grade_pilot_requires_explicit_nutrient_goal"
                    if nutrition_policy == WITHIN_GRADE_POLICY else "supported_nutrient_goal_required"]
                return result
            # A positive nutrient can improve even when it earns no negative points.
            items = (nutri_score_breakdown.get("negative_points") or {}).get("items") or {}
            available = {key: item for key, item in items.items()
                if (not preferred or key in preferred) and (_number(item.get("value_per_100g")) or 0) > 0}
            target = _identify_target_nutrient({"negative_points": {"items": available}}, preferred, minimum_points=0)
        else:
            target = _identify_target_nutrient(nutri_score_breakdown, preferred)
        if target is None:
            result.update(status="no_target", reasons=["requested_pilot_nutrient_unavailable_or_zero"
                if nutrition_policy == WITHIN_GRADE_POLICY else "requested_nutrient_unavailable_or_zero"
                if nutrition_policy != STRICT_POLICY else "no_negative_nutrient_above_production_threshold"])
            return result
        result["objective"] = {"metric": target["abs_key"], "direction": "decrease",
            "target_negative_points": target["points"], "score_key": target["score_key"],
            "selection_basis": "active_profile_negative_nutri_score_points",
            "nutri_score_improvement": "not_evaluated"}
        if nutrition_policy != STRICT_POLICY:
            result["objective"].update(benefit_policy=nutrition_policy,
                selection_basis="explicit_nutrient_goal_with_final_benefit_thresholds_pending" if preferred
                    else "positive_nutrient_ranked_by_score_points_with_final_benefit_thresholds_pending")
    elif mode == "cost":
        from recipe_wrangler.pricing.cost_calculator import SUPPORTED_COST_REGIONS, load_cost_catalogue
        if not cost_country or cost_country.upper() not in SUPPORTED_COST_REGIONS:
            result["reasons"] = ["explicit_supported_cost_country_required"]
            return result
        catalogue = load_cost_catalogue()
        result["objective"] = {"metric": "cost_eur", "direction": "decrease",
            "minimum_relative_reduction": COST_MIN_REDUCTION_PCT, "country": cost_country.upper()}
    else:
        result["objective"] = {"metric": "co2e_kg", "direction": "decrease",
            "minimum_relative_reduction": SUSTAINABILITY_MIN_REDUCTION_PCT}
        from recipe_wrangler.repositories import sustainability_references as references
        emissions, reference_status = references.lookup_references(list(dict.fromkeys(
            item.name for item in effective.ingredients if item.quantity.weight_g > 0)))

    for ingredient in effective.ingredients:
        reason, contribution, provenance = None, None, {}
        if not supported_quantity(effective, ingredient):
            reason = "raw_edible_quantity_evidence_required"
        elif ingredient.quantity.weight_g == 0:
            contribution = 0.0
        elif mode == "nutrition":
            composition = ingredient.composition
            if not supported_reporting_basis(effective, composition):
                reason = "supported_composition_basis_required"
            else:
                state = composition.properties.get("preparation_state")
                value = _number(composition.values_per_100g.get(target["abs_key"]))
                if not estimated_inputs(effective) and (state is None or state.value != "raw" or state.evidence.status != "stated"):
                    reason = "raw_composition_evidence_required"
                elif value is None:
                    reason = "target_nutrient_unknown"
                else:
                    contribution = ingredient.quantity.weight_g / 100 * value
                    provenance = {"composition": composition.ref.model_dump(mode="json"), "status": "stated"}
        elif mode == "cost":
            match = catalogue.resolve(ingredient.name, cost_country.upper())
            factor = _number(match.get("economic_reference_price_eur_kg")) if match.get("match_status") == "matched" else None
            if factor is None:
                reason = "price_unknown"
            else:
                contribution = ingredient.quantity.weight_g / 1000 * factor
                provenance = {"status": "proxy", "match": match, "factor_per_kg": factor}
        else:
            record = emissions.get(ingredient.name)
            valid = record is not None and references.select_reference(ingredient.name, [record]) is not None
            factor = _number(record["metrics"]["climate_change"]["value"]) if valid else None
            if factor is None:
                reason = "emissions_factor_unknown" if reference_status == "available" else reference_status
            else:
                contribution = ingredient.quantity.weight_g / 1000 * factor
                provenance = {"status": "proxy", "reference": record,
                    "policy_version": references.POLICY_VERSION, "factor_per_kg": factor}
        if reason:
            result["unknown_contributions"].append({"position": ingredient.position, "name": ingredient.name, "reason": reason})
        elif contribution is not None:
            result["supported_partial_total"] += contribution
            if contribution > 0:
                result["ranked_targets"].append({"position": ingredient.position, "name": ingredient.name,
                    "contribution": contribution, "provenance": provenance})
    if not result["unknown_contributions"]:
        result["complete_total"] = result["supported_partial_total"]
    result["ranked_targets"].sort(key=lambda item: (-item["contribution"], item["position"]))
    for rank, item in enumerate(result["ranked_targets"], 1):
        item.update(rank=rank, within_production_search_limit=rank <= MAX_ENHANCEMENT_TARGETS)
    result["status"] = "targets_identified" if result["ranked_targets"] else (
        "needs_evidence" if result["unknown_contributions"] else "no_target")
    if result["unknown_contributions"]:
        result["reasons"].append("target_ranking_has_incomplete_coverage")
    result["required_execution_checks"] = ["supported_edit_operation", "source_compatible_candidates",
        "equivalent_quantities_and_nutritional_bases", "allergen_and_dietary_constraints",
        "complete_objective_improvement", "nutri_score_no_regression", "culinary_suitability",
        "validated_instruction_edit", "active_profile_and_recipe_text_unchanged"]
    if mode != "nutrition":
        result["required_execution_checks"].append("replacement_specific_factor_provenance")
    return result


def plan_active_enhancement(
    recipe_id: str, nutrition_source: NutritionSource, *, mode: EnhancementMode,
    goal_nutrients: list[str] | None = None, cost_country: str | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
) -> dict:
    """Resolve the active baseline for a button-style request, without paid calls."""
    from recipe_wrangler.repositories import matching_store
    from .comparison_store import ComparisonLoadError, load_active_snapshot

    snapshot = load_active_snapshot(recipe_id, nutrition_source)
    row = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
    if row is None or row["run_id"] != snapshot.run_id:
        raise ComparisonLoadError("active_profile_changed", blocked=True)
    result = plan_enhancement(snapshot, mode=mode,
        nutri_score_breakdown=(row.get("profile") or {}).get("nutri_score_breakdown"),
        goal_nutrients=goal_nutrients, cost_country=cost_country, quantity_declarations=quantity_declarations)
    current = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
    if current is None or current["run_id"] != snapshot.run_id:
        raise ComparisonLoadError("active_profile_changed", blocked=True)
    return result
