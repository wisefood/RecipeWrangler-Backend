"""Recipe-to-card orchestration: shortlist, context, recalculate, edit, assemble."""

from collections.abc import Callable, Sequence
import math
import re

from recipe_wrangler.repositories import matching_store
from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
from recipe_wrangler.matching.source_context import recipe_revision, projected_occurrences, source_line_key

from .models import ComparisonComposition, ComparisonConstraints, ComparisonSnapshot, NutritionSource, QuantityBasisDeclaration
from .comparison_store import ComparisonLoadError, load_active_snapshot
from .enhancement import EnhancementMode
from .enhancement_screening import screen_enhancement
from .enhancement_response import finalize_enhancement_response
from .instruction_editor import draft_raw_part_instructions
from .objective_approval import InputScoreContext, approve_nutrition_candidate, production_score_context, supported_score_context
from .preview import preview_raw_part_candidates
from .models import NutrientObjective
from .recipe_response import assemble_adaptation_response
from .nutrition_benefit import NutritionPolicy, DEFAULT_POLICY


def _capabilities() -> dict:
    return {"executable": ["same_identity_food_part_variant", "evidence_backed_raw_ingredient_substitution", "same_identity_nutritional_facet_variant", "existing_cooking_duration_or_temperature_adjustment"], "not_yet_executable": {
        "cooking_method_change": "Requires validated edible masses, cooking yields, retention, added fat/water and step edits",
        "quantity_change": "Requires validated portion and culinary-role effects and quantity/instruction edits",
        "cooked_or_unverified_ingredient_substitution": "Requires equivalent quantity bases and cooking/portion evidence"}}


def enhance_recipe(
    snapshot: ComparisonSnapshot, *, title: str, instructions: Sequence[str],
    mode: EnhancementMode = "nutrition", nutri_score_breakdown: dict | None = None,
    score_context: InputScoreContext | None = None, goal_nutrients: list[str] | None = None,
    cost_country: str | None = None, constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    check_current: Callable[[], None] | None = None,
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
) -> dict:
    """Use trusted recipe inputs; active persistence/text checks live in the adapter.

    At most one role call, one candidate batch and one editor call. Additional
    model-proposed quantities/methods remain conditional; bounded existing cooking
    parameters can enter an unsaved draft after structural checks.
    """
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    instructions = list(instructions)
    constraints = ComparisonConstraints.model_validate((constraints or ComparisonConstraints()).model_dump())
    mass = sum(item.quantity.weight_g for item in snapshot.ingredients)
    preflight_context = score_context if (score_context is not None and supported_score_context(score_context)
        and (score_context.recipe_id, score_context.baseline_run_id, score_context.quantity_input_hash) == (
            snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash)
        and math.isfinite(mass) and math.isclose(mass, score_context.total_weight_g, rel_tol=1e-9, abs_tol=1e-9)) else None
    screening = screen_enhancement(snapshot, mode=mode, nutri_score_breakdown=nutri_score_breakdown,
        goal_nutrients=goal_nutrients, cost_country=cost_country, constraints=constraints,
        quantity_declarations=quantity_declarations, allow_general_substitutions=True, require_score_inputs=True,
        score_context=preflight_context, title=title, instructions=instructions, nutrition_policy=nutrition_policy)
    result = {"pipeline_version": "recipe_enhancement_v1", "mode": mode,
        "original_recipe_id": snapshot.recipe_id, "baseline_run_id": snapshot.run_id,
        "quantity_input_hash": snapshot.quantity_input_hash, "status": screening["status"],
        "reasons": list(screening["reasons"]), "screening": screening, "capabilities": _capabilities(),
        "stages": ["deterministic_screening"], "preview": None, "objective_decisions": [],
        "final_objective_approval": None, "adapted_recipe": None, "changes": [],
        "model_call_limit": 3, "model_calls": [], "instruction_decisions": [],
        "constraints": constraints.model_dump(mode="json"), "recipe_ready": False, "saved": False}
    def before_request(stage: str, candidate_id: str | None = None) -> None:
        if len(result["model_calls"]) >= result["model_call_limit"]:
            raise ValueError("model_call_budget_exhausted")
        result["model_calls"].append({"stage": stage, "target_position": screening["selected_target_position"],
            "candidate_id": candidate_id})

    def remaining_targets() -> bool:
        attempted = {item["position"] for item in screening["attempts"]}
        return any(item["within_production_search_limit"] and item["position"] not in attempted
            for item in screening["target_plan"].get("ranked_targets", []))
    def current() -> bool:
        if check_current is None:
            return True
        try:
            check_current()
            return True
        except ComparisonLoadError as error:
            result.update(status="blocked" if error.blocked else "needs_evidence", reasons=[error.reason],
                adapted_recipe=None, changes=[], selected_candidate_id=None)
            return False
    if screening["status"] != "candidates_screened":
        return finalize_enhancement_response(result)
    # Invalid contexts never authorize the early filter or a paid request.
    if score_context is None:
        result.update(status="needs_evidence", reasons=["verified_input_score_context_required"])
        return finalize_enhancement_response(result)
    score_context = InputScoreContext.model_validate(score_context.model_dump())
    if (score_context.recipe_id, score_context.baseline_run_id, score_context.quantity_input_hash) != (
        snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash):
        result.update(status="blocked", reasons=["score_context_baseline_mismatch"])
        return finalize_enhancement_response(result)
    if not supported_score_context(score_context):
        result.update(status="needs_evidence", reasons=["stated_score_context_required"])
        return finalize_enhancement_response(result)
    mass = sum(item.quantity.weight_g for item in snapshot.ingredients)
    if not math.isfinite(mass) or not math.isclose(mass, score_context.total_weight_g, rel_tol=1e-9, abs_tol=1e-9):
        result.update(status="blocked", reasons=["score_context_mass_mismatch"])
        return finalize_enhancement_response(result)
    objective = NutrientObjective.model_validate(screening["numerical_objective"])
    selected_attempt = next(item for item in screening["attempts"] if item["position"] == screening["selected_target_position"])
    selected_refs = screening["candidate_refs"]
    candidates = [item["comparison"]["target_composition"] for item in selected_attempt["shortlist"]["screening"]
                  if item["composition"] in selected_refs]
    candidates = [ComparisonComposition.model_validate(item) for item in candidates]
    if not current():
        return finalize_enhancement_response(result)
    preview = preview_raw_part_candidates(snapshot, candidates, title=title, instructions=instructions,
        target_position=screening["selected_target_position"], objective=objective,
        constraints=constraints, quantity_declarations=quantity_declarations, before_request=before_request)
    result["stages"].append("culinary_context_assessment")
    result["preview"] = preview.model_dump(mode="json")
    if not current():
        return finalize_enhancement_response(result)
    ready = sorted((item for item in preview.candidates if item.status == "ready_for_instruction_editing"),
                   key=lambda item: item.rank)
    if not ready:
        result.update(status=preview.status, reasons=list(preview.reasons) or ["no_culinarily_approved_candidate"])
        failures = [item.failure_reason for item in (preview.roles, preview.culinary_batch)
            if item is not None and item.status == "unavailable" and item.failure_reason]
        if failures:
            result.update(status="unavailable", reasons=failures)
        elif remaining_targets():
            result.update(status="needs_evidence", reasons=result["reasons"] + ["model_call_budget_prevents_target_fallback"])
        return finalize_enhancement_response(result)
    result["stages"].append("nutrition_recalculation_and_final_approval")
    response = None
    for candidate in ready:
        approval = approve_nutrition_candidate(snapshot, candidate.comparison, objective, score_context,
            constraints=constraints, nutrition_policy=nutrition_policy)
        result["objective_decisions"].append({"candidate_id": candidate.candidate_id, **approval})
        if approval["status"] == "blocked":
            result.update(status="blocked", reasons=approval["reasons"])
            return finalize_enhancement_response(result)
        if approval["status"] != "approved_input_objective":
            continue
        result["final_objective_approval"] = approval
        if not current():
            return finalize_enhancement_response(result)
        draft = draft_raw_part_instructions(snapshot, preview, candidate.candidate_id, title=title, instructions=instructions,
            before_request=lambda stage: before_request(stage, candidate.candidate_id))
        result["instruction_decisions"].append({"candidate_id": candidate.candidate_id,
            "status": draft.status, "reasons": list(draft.reasons)})
        if "instruction_editing" not in result["stages"]:
            result["stages"].append("instruction_editing")
        response = assemble_adaptation_response(snapshot, preview, draft)
        if not current():
            return finalize_enhancement_response(result)
        # Local refusals consume no call; another candidate may use the reserved editor call.
        if draft.status == "needs_evidence" and len(result["model_calls"]) < result["model_call_limit"]:
            continue
        if response.adapted_recipe is not None:
            response.adapted_recipe.nutrition.input_score_estimate = approval
            response.adapted_recipe.impact_estimate = approval.get("impact_estimate")
            if response.adapted_recipe.impact_estimate:
                response.uncertainties.append("Cost/emissions changes are catalogue reference estimates, not measured recipe impacts or retail quotes.")
            if score_context.evidence.status == "proxy":
                response.uncertainties.append("Input score reuses production's estimated food type and FVL classification; these remain proxy assumptions.")
            if approval.get("benefit_policy") in {DEFAULT_POLICY, "within_grade_pilot_v1"}:
                response.uncertainties.append("Uses a nutrition benefit policy with provisional thresholds; unchanged grade is allowed. Nutrient changes are input estimates, not measured finished-dish benefits.")
            if approval.get('accepted_tradeoffs'):
                response.uncertainties.append("Small nutrient trade-offs were accepted within the prototype's 5% limit; see accepted_tradeoffs in the input score estimate.")
            if approval.get('unassessed_nutrients'):
                response.uncertainties.append("Some carbohydrate/total-fat comparisons remain unknown; these were not used to approve the nutrient goal. See unassessed_nutrients and the card's ingredient gaps.")
        break
    if response is None:
        result.update(status="needs_evidence" if any(item["status"] == "needs_evidence" for item in result["objective_decisions"])
                      else "no_supported_candidates", reasons=["no_culinarily_suitable_candidate_passed_final_nutrition_gate"])
        return finalize_enhancement_response(result)
    result.update(response.model_dump(mode="json"))
    if response.adapted_recipe is None and draft.status == "needs_evidence" and (
            len(ready) > 1 or remaining_targets()):
        result["reasons"].append("model_call_budget_exhausted" if len(result["model_calls"]) >= result["model_call_limit"]
            else "model_call_budget_prevents_target_fallback" if remaining_targets() else "no_editable_candidate_in_shortlist")
    result["stages"].append("recipe_card_assembly")
    return finalize_enhancement_response(result)


def _recipe_revision(recipe: dict) -> str:
    return matching_store.digest([recipe_revision(recipe),recipe.get('diet_tags'),recipe.get('tags')])


def _verify_source_binding(snapshot, recipe, profile):
    originals = [item.original_text or (item.source_evidence.source_line if item.source_evidence else "")
                 for item in snapshot.ingredients]
    from recipe_wrangler.matching.source_context import split_seasoning_bindings, unambiguous_missing_source_bindings
    seasoning_bindings = split_seasoning_bindings(snapshot.ingredients, recipe.get("original_ingredients") or [])
    if not all(originals):
        if seasoning_bindings is None:
            recovered = unambiguous_missing_source_bindings(snapshot.ingredients, recipe.get("original_ingredients") or [])
            if recovered is None:
                raise ComparisonLoadError("profile_original_ingredient_text_incomplete")
            originals = [binding['source_text'] for binding in recovered]
        else:
            recovered = seasoning_bindings
            originals = [binding['source_text'] for binding in seasoning_bindings]
    pinned = profile.get("source_recipe_revision")
    if pinned:
        if pinned != recipe_revision(recipe):
            raise ComparisonLoadError("recipe_text_changed_since_source_repair", blocked=True)
        rows = projected_occurrences(snapshot.recipe_id, snapshot.run_id, snapshot.nutrition_source)
        expected = [{"position": item.position, "ingredient": item.name, "original_text": text,
            "weight_g": item.quantity.weight_g, "quantity_input_hash": snapshot.quantity_input_hash,
            "source_recipe_revision": pinned} for item,text in zip(snapshot.ingredients,originals,strict=True)]
        if rows != expected:
            raise ComparisonLoadError("canonical_occurrence_projection_not_synchronized")
    else:
        if seasoning_bindings is not None:
            return seasoning_bindings
        if not all(item.original_text or (item.source_evidence.source_line if item.source_evidence else '')
                   for item in snapshot.ingredients):
            return recovered
        if len(originals) != len(recipe.get("original_ingredients") or []):
            source_keys = [source_line_key(text) for text in recipe.get("original_ingredients") or []]
            if len(set(source_keys)) != len(source_keys):
                raise ComparisonLoadError("source_recipe_repeated_lines_require_verification")
            raise ComparisonLoadError("source_recipe_occurrence_alignment_required")
        saved_keys = [source_line_key(text) for text in originals]
        current_keys = [source_line_key(text) for text in recipe["original_ingredients"]]
        if saved_keys != current_keys:
            if sorted(saved_keys) != sorted(current_keys):
                raise ComparisonLoadError("recipe_ingredient_text_does_not_match_profile", blocked=True)
            if len(set(saved_keys)) != len(saved_keys):
                raise ComparisonLoadError("source_recipe_reordered_repeated_lines_require_verification")
            positions = {key: index for index, key in enumerate(current_keys)}
            return [{"profile_position": item.position, "source_position": positions[key],
                "source_text": recipe["original_ingredients"][positions[key]],
                "method": "unique_complete_source_line"}
                for item, key in zip(snapshot.ingredients, saved_keys, strict=True)]


def enhance_active_recipe(
    recipe_id: str, nutrition_source: NutritionSource, *, mode: EnhancementMode = "nutrition",
    score_context: InputScoreContext | None = None, goal_nutrients: list[str] | None = None,
    cost_country: str | None = None, constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    nutrition_policy: NutritionPolicy = DEFAULT_POLICY,
) -> dict:
    """Load the recipe automatically and pin both its text and active nutrition run."""
    snapshot, result = None, None
    try:
        snapshot = load_active_snapshot(recipe_id, nutrition_source, allow_input_estimates=True)
        recipe = fetch_recipe_info_by_id(recipe_id)
        row = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
        if row is None or row["run_id"] != snapshot.run_id:
            raise ComparisonLoadError("active_profile_changed", blocked=True)
        if not recipe or str(recipe.get("recipe_id")) != recipe_id:
            raise ComparisonLoadError("authoritative_recipe_not_found")
        source_bindings = _verify_source_binding(snapshot,recipe,row["profile"])
        try:
            serves = float(recipe.get("serves"))
        except (TypeError, ValueError) as error:
            raise ComparisonLoadError("authoritative_recipe_servings_missing") from error
        if serves != snapshot.serves:
            raise ComparisonLoadError("recipe_servings_do_not_match_profile", blocked=True)
        if not recipe.get("title") or not recipe.get("instructions"):
            raise ComparisonLoadError("authoritative_recipe_text_missing")
        revision = _recipe_revision(recipe)
        if score_context is None:
            score_context = production_score_context(snapshot, row.get("profile") or {})
        def check_current():
            current = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
            if current is None or current["run_id"] != snapshot.run_id:
                raise ComparisonLoadError("active_profile_changed", blocked=True)
            if _recipe_revision(fetch_recipe_info_by_id(recipe_id)) != revision:
                raise ComparisonLoadError("recipe_text_changed", blocked=True)
            if row['profile'].get('source_recipe_revision'):
                _verify_source_binding(snapshot,recipe,row['profile'])
        result = enhance_recipe(snapshot, title=recipe["title"], instructions=recipe["instructions"], mode=mode,
            nutri_score_breakdown=(row.get("profile") or {}).get("nutri_score_breakdown"),
            score_context=score_context, goal_nutrients=goal_nutrients, cost_country=cost_country,
            constraints=constraints, quantity_declarations=quantity_declarations, check_current=check_current,
            nutrition_policy=nutrition_policy)
        check_current()
        result["recipe_revision"] = revision
        if source_bindings:
            result.setdefault("provenance", {})["source_occurrence_bindings"] = source_bindings
        return finalize_enhancement_response(result)
    except ComparisonLoadError as error:
        failure = {"pipeline_version": "recipe_enhancement_v1", "mode": mode, "original_recipe_id": recipe_id,
            "status": "blocked" if error.blocked else "needs_evidence", "reasons": [error.reason],
            "adapted_recipe": None, "changes": [], "recipe_ready": False, "saved": False,
            "baseline_run_id": snapshot.run_id if snapshot else None,
            "quantity_input_hash": snapshot.quantity_input_hash if snapshot else None,
            "selected_candidate_id": None,
            "constraints": (constraints or ComparisonConstraints()).model_dump(mode="json"), "capabilities": _capabilities()}
        return finalize_enhancement_response({**(result or {}), **failure})
