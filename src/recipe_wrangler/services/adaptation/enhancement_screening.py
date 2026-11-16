"""Connect production-style targets to bounded deterministic candidate screening."""

from collections.abc import Sequence
from copy import deepcopy
import math

from recipe_wrangler.repositories import matching_store

from .candidate_selection import shortlist_raw_part_candidates
from .models import (
    ComparisonConstraints, ComparisonIngredient, ComparisonSnapshot, NutritionSource, QuantityBasisDeclaration,
)
from .comparison_store import ComparisonLoadError, load_active_snapshot
from .enhancement import EnhancementMode, plan_enhancement
from .models import NutrientObjective
from .impact_objectives import build_impact_objective
from .quantity_declarations import apply_quantity_declarations
from .contextual_assessment import build_adaptation_context
from .objective_approval import InputScoreContext
from .nutrition_benefit import NutritionPolicy, DEFAULT_POLICY


def _discover_operation(ingredient: ComparisonIngredient, allow_general: bool = False,
                       input_estimate: bool = False, metric: str | None = None) -> tuple[str | None, str]:
    """Use source facts for discovery only; the comparison checks observations.

    No name/graph inference can establish an executable substitution. In
    particular, discovery never fills missing occurrence facts from the source.
    """
    if ingredient.composition is None:
        return None, "source_composition_missing"
    from .nutritional_variants import stated, FACETS
    identity = stated(ingredient.composition, "base_identity")
    if not isinstance(identity, str) or identity in {"", "unknown"}:
        return None, "operation_source_evidence_missing:base_identity"
    if stated(ingredient.composition, "preparation_state") != "raw":
        if allow_general and input_estimate and metric in FACETS:
            return "nutritional_product_variant", "same_identity_nutritional_facet_discovery"
        return None, "operation_source_evidence_missing:raw_state"
    for key in ("biological_source", "food_part"):
        assertion = ingredient.composition.properties.get(key)
        if (assertion is None or assertion.value is None or assertion.value in ("", "unknown")
                or assertion.evidence.status != "stated"):
            if allow_general and input_estimate:
                return "raw_ingredient_substitution", "production_candidate_discovery"
            return None, "operation_source_evidence_missing:" + key
    return "food_part_variant", "source_operation_discovered"


def screen_enhancement(
    snapshot: ComparisonSnapshot, *, mode: EnhancementMode,
    nutri_score_breakdown: dict | None = None, goal_nutrients: list[str] | None = None,
    cost_country: str | None = None, constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    allow_general_substitutions: bool = False,
    require_score_inputs: bool = False,
    score_context: InputScoreContext | None = None,
    title: str | None = None, instructions: Sequence[str] | None = None,
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
    _allow_graph_fallback: bool = True,
) -> dict:
    """Try up to ten ranked contributors, stopping at the first shortlist.

    Supplied score/text contexts allow deterministic eligibility checks before
    the shortlist limit. Final approval still repeats after culinary assessment.
    """
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    constraints = ComparisonConstraints.model_validate((constraints or ComparisonConstraints()).model_dump())
    plan = plan_enhancement(snapshot, mode=mode, nutri_score_breakdown=nutri_score_breakdown,
        goal_nutrients=goal_nutrients, cost_country=cost_country, quantity_declarations=quantity_declarations,
        nutrition_policy=nutrition_policy)
    result = {"version": "enhancement_screening_v1", "recipe_id": snapshot.recipe_id,
        "baseline_run_id": snapshot.run_id, "quantity_input_hash": snapshot.quantity_input_hash,
        "mode": mode, "status": plan["status"], "reasons": list(plan["reasons"]), "target_plan": plan,
        "constraints": constraints.model_dump(mode="json"), "attempts": [],
        "selected_target_position": None, "candidate_refs": [], "numerical_objective": None,
        "objective_approval": "pending", "execution_status": "not_executed",
        "adapted_recipe": None, "recipe_ready": False, "saved": False}
    def finish_failed_objective(*, preferred_only=False):
        if mode != 'nutrition' or goal_nutrients or nutrition_policy != DEFAULT_POLICY:
            return result
        current = (plan.get('objective') or {}).get('score_key')
        items = ((nutri_score_breakdown or {}).get('negative_points') or {}).get('items') or {}
        def positive(key):
            value = (items.get(key) or {}).get('value_per_100g')
            return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
        choices = sorted((key for key in ('energy', 'sugar', 'saturated_fats', 'sodium')
            if key != current and positive(key)),
            key=lambda key: (items[key].get('points') or 0), reverse=True)
        scope = 'preferred_catalogue' if preferred_only or not _allow_graph_fallback else 'catalogue_and_graph'
        audit = [{'objective': result['numerical_objective'], 'status': result['status'],
                  'search_scope': scope, 'attempts': deepcopy(result['attempts'])}]
        for goal in choices:
            alternate = screen_enhancement(snapshot, mode=mode, nutri_score_breakdown=nutri_score_breakdown,
                goal_nutrients=[goal], cost_country=cost_country, constraints=constraints,
                quantity_declarations=quantity_declarations, allow_general_substitutions=allow_general_substitutions,
                require_score_inputs=require_score_inputs, score_context=score_context, title=title,
                instructions=instructions, nutrition_policy=nutrition_policy,
                _allow_graph_fallback=_allow_graph_fallback and not preferred_only)
            audit.append({'objective': alternate['numerical_objective'], 'status': alternate['status'],
                          'search_scope': scope, 'attempts': alternate['attempts']})
            if alternate['status'] == 'candidates_screened':
                alternate['objective_attempts'] = audit
                alternate['target_plan']['objective']['selection_basis'] = (
                    'automatic_fallback_after_failed_preferred_catalogue_screening' if preferred_only
                    else 'automatic_fallback_after_failed_nutrient_objective')
                alternate['reasons'].append('alternative_nutrient_objective_selected_after_failed_screening')
                return alternate
        result['objective_attempts'] = audit
        return result
    if plan["status"] != "targets_identified":
        return finish_failed_objective()
    objective = NutrientObjective(metric=plan["objective"]["metric"]) if mode == "nutrition" else None
    result["numerical_objective"] = objective.model_dump(mode="json") if objective else None
    by_position = {item.position: item for item in snapshot.ingredients}
    targets = [target for target in plan['ranked_targets'] if target['within_production_search_limit']]
    quick_first = mode == 'nutrition' and allow_general_substitutions and snapshot.input_policy == 'production_estimate'
    searches = [(target, False) for target in targets] if quick_first or not _allow_graph_fallback else []
    if _allow_graph_fallback:
        searches += [(target, True) for target in targets]
    for target, graph_fallback in searches:
        if quick_first and graph_fallback and target is targets[0]:
            # A calorie miss must not hide a supported fat/sodium benefit already
            # available from the catalogue, even when the score stays unchanged.
            result['status'] = ('needs_evidence' if any(item['status'] == 'needs_evidence'
                for item in result['attempts']) else 'no_supported_candidates')
            alternate = finish_failed_objective(preferred_only=True)
            if alternate['status'] == 'candidates_screened':
                return alternate
        operation, reason = _discover_operation(by_position[target["position"]], allow_general_substitutions,
            snapshot.input_policy == "production_estimate", objective.metric if objective else None)
        attempt = {"position": target["position"], "rank": target["rank"], "operation": operation,
            "status": "unsupported_operation", "reasons": [reason], "shortlist": None}
        previous = next((item for item in result['attempts'] if item['position'] == target['position']), None)
        if previous is not None:
            previous.update(attempt)
            attempt = previous
            attempt['search_phases'].append('graph_fallback')
        else:
            attempt['search_phases'] = ['preferred_catalogue' if quick_first else 'catalogue_and_graph']
            result["attempts"].append(attempt)
        if operation is None:
            if "missing" in reason:
                attempt["status"] = "needs_evidence"
            continue
        if mode != "nutrition":
            effective, _ = apply_quantity_declarations(snapshot, quantity_declarations)
            baseline_objective, missing = build_impact_objective(effective, [], mode=mode, country=cost_country)
            if baseline_objective is None:
                attempt.update(status="needs_evidence", reasons=["replacement_specific_objective_comparison_required"],
                    impact_missing_evidence=missing)
                continue
            from . import candidate_selection
            from pydantic import ValidationError
            if operation == "raw_ingredient_substitution":
                from .production_candidates import production_candidate_records
                rows, _ = production_candidate_records(snapshot, target["position"])
            else:
                from .production_candidates import food_part_candidate_records
                rows, _ = food_part_candidate_records(snapshot, target["position"])
            pool = []
            for row in rows:
                try:
                    pool.append(candidate_selection._composition(row))
                except (ComparisonLoadError, ValidationError, KeyError, TypeError):
                    continue
            objective, missing = build_impact_objective(effective, pool, mode=mode, country=cost_country)
            if objective is None:
                attempt.update(status="needs_evidence", reasons=missing)
                continue
            result["numerical_objective"] = objective.model_dump(mode="json")
        shortlist = shortlist_raw_part_candidates(snapshot, target_position=target["position"],
            objective=objective, constraints=constraints, quantity_declarations=quantity_declarations,
            allow_general_substitutions=allow_general_substitutions, require_score_inputs=require_score_inputs,
            allow_graph_fallback=graph_fallback,
            score_context=score_context, nutrition_policy=nutrition_policy, instruction_context=build_adaptation_context(snapshot,
                title=title, instructions=instructions, target_position=target["position"],
                objective=f"{objective.direction} {objective.metric} per serving while preserving culinary function.",
                constraints=constraints) if title is not None and instructions is not None else None)
        attempt.update(status=shortlist.status, reasons=list(shortlist.reasons), shortlist=shortlist.model_dump(mode="json"))
        if shortlist.status == "selected":
            refs = shortlist.candidate_refs
            result.update(status="candidates_screened", selected_target_position=target["position"],
                candidate_refs=[item.model_dump(mode="json") for item in refs])
            result["reasons"].append("final_objective_approval_required_after_culinary_assessment")
            return result
    result["status"] = "needs_evidence" if plan["unknown_contributions"] or any(
        item["status"] == "needs_evidence" for item in result["attempts"]) else "no_supported_candidates"
    result["reasons"].append("search_budget_reached" if any(
        not target["within_production_search_limit"] for target in plan["ranked_targets"]) or any(
        reason.endswith("scan_limit_reached") for attempt in result["attempts"] for reason in attempt["reasons"])
        else "no_supported_adaptation_after_implemented_search")
    return finish_failed_objective()


def screen_active_enhancement(
    recipe_id: str, nutrition_source: NutritionSource, *, mode: EnhancementMode,
    goal_nutrients: list[str] | None = None, cost_country: str | None = None,
    constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
) -> dict:
    """Load and pin the entire target-to-shortlist operation to the active run."""
    snapshot = load_active_snapshot(recipe_id, nutrition_source)
    row = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
    if row is None or row["run_id"] != snapshot.run_id:
        raise ComparisonLoadError("active_profile_changed", blocked=True)
    result = screen_enhancement(snapshot, mode=mode,
        nutri_score_breakdown=(row.get("profile") or {}).get("nutri_score_breakdown"),
        goal_nutrients=goal_nutrients, cost_country=cost_country, constraints=constraints,
        quantity_declarations=quantity_declarations)
    current = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
    if current is None or current["run_id"] != snapshot.run_id:
        raise ComparisonLoadError("active_profile_changed", blocked=True)
    return result
