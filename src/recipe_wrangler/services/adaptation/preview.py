"""Bounded previews joining deterministic nutrition and inferred culinary context."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math

from pydantic import ValidationError

from recipe_wrangler.repositories import matching_store

from .comparison import compare_raw_part
from .models import (
    ComparisonComposition, ComparisonConstraints, ComparisonSnapshot, CompositionRef,
    Evidence, NutritionSource, QuantityBasisDeclaration, RawPartEdit,
)
from .comparison_store import ComparisonLoadError, _composition, load_active_snapshot
from .contextual_assessment import (
    _context_payload, assess_contextual_candidates, build_adaptation_context, infer_contextual_roles,
)
from .models import AdaptationContext, CandidateBatchResult, ContextCandidate, RoleInferenceResult
from .models import AdaptationPreview, NutrientObjective, PreviewCandidate
from .impact_objectives import objective_delta
from .production_candidates import make_substitution_edit
from .cooking_parameters import cooking_parameter_slots


def _candidate_id(ref: CompositionRef) -> str:
    return matching_store.digest(ref.model_dump(mode="json"))


def _check_pool(refs: Sequence[CompositionRef]) -> None:
    ids = [_candidate_id(ref) for ref in refs]
    if len(ids) > 10 or len(ids) != len(set(ids)):
        raise ValueError("Provide at most ten unique released compositions")


def _summarize(preview: AdaptationPreview) -> AdaptationPreview:
    ready = [item for item in preview.candidates if item.status == "ready_for_instruction_editing"]
    ready.sort(key=lambda item: (-item.objective_improvement_per_serving, item.candidate_id))
    for rank, item in enumerate(ready, 1):
        item.rank = rank
    states = {item.status for item in preview.candidates}
    preview.status = ("has_supported_candidates" if ready else "needs_evidence" if "needs_evidence" in states
                      else "changes_required" if "needs_changes" in states else "no_supported_candidates")
    if not preview.candidates and preview.candidate_selection and preview.candidate_selection.status == "needs_evidence":
        preview.status = "needs_evidence"
    return preview


def _check_batch(context: AdaptationContext, roles: RoleInferenceResult, batch: CandidateBatchResult,
                 candidates: list[ContextCandidate]) -> None:
    """Reject stale/replayed wrappers before using their culinary decisions."""
    context_hash = matching_store.digest(_context_payload(context))
    if (roles.context_hash != context_hash or batch.context_hash != context_hash
            or roles.target_position != context.target_position or batch.target_position != context.target_position
            or batch.role_assessment_hash != roles.assessment_hash
            or batch.candidate_batch_hash != matching_store.digest([item.model_dump(mode="json") for item in candidates])):
        raise ValueError("assessment_input_mismatch")
    if roles.assessment is not None and roles.assessment_hash != matching_store.digest(roles.assessment.model_dump(mode="json")):
        raise ValueError("changed_role_assessment")
    expected = {item.candidate_id for item in candidates}
    returned = [item.candidate_id for item in batch.assessments]
    if batch.status in {"assessed", "uncertain"} and batch.failure_reason is None:
        if len(returned) != len(set(returned)) or set(returned) != expected or batch.excluded_candidates:
            raise ValueError("assessment_partition_mismatch")


def preview_raw_part_candidates(
    snapshot: ComparisonSnapshot, candidates: Sequence[ComparisonComposition], *,
    title: str, instructions: Sequence[str], target_position: int, objective: NutrientObjective,
    constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
    before_request: Callable[[str], None] | None = None,
) -> AdaptationPreview:
    """Use trusted normalized source data; active-run checks belong to the adapter.

    Only the declared per-serving nutrient is ranked. Additional operations and
    finished cooking outcomes remain outside the single-variant comparison.
    """
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    candidates = [ComparisonComposition.model_validate(item.model_dump()) for item in candidates]
    objective = NutrientObjective.model_validate(objective.model_dump())
    _check_pool([item.ref for item in candidates])
    constraints = constraints or ComparisonConstraints()
    context = build_adaptation_context(snapshot, title=title, instructions=instructions,
        objective=f"{objective.direction} {objective.metric} per serving while preserving culinary function.",
        target_position=target_position, constraints=constraints)
    preview = AdaptationPreview(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, target_position=target_position,
        objective=objective.model_copy(deep=True), status="no_supported_candidates", context=context)
    original = next(item for item in snapshot.ingredients if item.position == target_position)
    eligible = []
    for target in candidates:
        item = PreviewCandidate(candidate_id=_candidate_id(target.ref), composition=target.ref.model_copy(deep=True),
            label=(target.provenance.get("candidate") or {}).get("document") or target.ref.candidate_id,
            status="needs_evidence")
        preview.candidates.append(item)
        if original.composition is None:
            item.reasons = ["source_composition"]
            continue
        try:
            edit = make_substitution_edit(snapshot, target_position, target, nutritional_metric=objective.metric)
        except ComparisonLoadError as error:
            item.reasons = [error.reason]
            continue
        comparison = compare_raw_part(snapshot, edit, target, constraints, quantity_declarations=quantity_declarations)
        item.comparison = comparison
        if comparison.status != "supported_input_comparison":
            item.status = "rejected" if comparison.status == "blocked" else "needs_evidence"
            item.reasons = comparison.blockers + comparison.missing_evidence
            continue
        delta = objective_delta(comparison, objective)
        if delta is None or not math.isfinite(delta):
            item.reasons = ["impact_reference_comparison_incomplete" if objective.impact_context else "objective_nutrient_incomplete_per_serving"]
            continue
        improvement = -delta if objective.direction == "decrease" else delta
        item.objective_improvement_per_serving = improvement
        if improvement <= 0:
            item.status, item.reasons = "rejected", ["no_objective_improvement"]
            continue
        eligible.append(ContextCandidate(candidate_id=item.candidate_id, label=item.label,
            composition=item.composition.model_copy(deep=True),
            planned_edit=comparison.edit.model_copy(deep=True),
            properties={key: value.model_copy(deep=True) for key, value in target.properties.items()},
            constraint_status="passed", constraint_evidence=[Evidence(
                reference=f"comparison/{snapshot.run_id}/{target_position}/{item.candidate_id}",
                method="deterministic_comparison_constraints", version=comparison.comparison_version, status="stated",
            )]))
    if not eligible:
        return _summarize(preview)
    request_options = {"before_request": before_request} if before_request is not None else {}
    roles = infer_contextual_roles(context, **request_options)
    batch = assess_contextual_candidates(context, roles, eligible, **request_options)
    try:
        roles = RoleInferenceResult.model_validate(roles.model_dump())
        batch = CandidateBatchResult.model_validate(batch.model_dump())
        _check_batch(context, roles, batch, eligible)
        preview.roles, preview.culinary_batch = roles, batch
        roles_ready = roles.status == "assessed" and roles.assessment is not None and roles.assessment.status == "inferred"
        usable = roles_ready and batch.status != "unavailable" and batch.failure_reason is None
        assessments = {item.candidate_id: item for item in batch.assessments} if usable else {}
    except ValueError:
        usable, assessments = False, {}
        preview.reasons.append("assessment_input_mismatch")
    eligible_ids = {item.candidate_id for item in eligible}
    for item in preview.candidates:
        if item.candidate_id not in eligible_ids:
            continue
        assessment = assessments.get(item.candidate_id)
        if not usable or assessment is None:
            item.reasons.append("culinary_assessment_unavailable_or_uncertain")
            continue
        item.culinary = assessment.model_copy(deep=True)
        if assessment.verdict == "unsuitable":
            item.status, item.reasons = "rejected", ["culinarily_unsuitable"]
        elif assessment.verdict == "uncertain":
            item.status, item.reasons = "needs_evidence", ["culinary_uncertainty"]
        elif assessment.verdict == "suitable_with_changes":
            if cooking_parameter_slots(context, assessment):
                item.status, item.reasons = "ready_for_instruction_editing", ["cooking_parameter_adjustments_pending"]
            else:
                item.status, item.reasons = "needs_changes", ["additional_operations_require_validation_and_recalculation"]
        else:
            item.status, item.reasons = "ready_for_instruction_editing", []
    return _summarize(preview)


def preview_active_raw_part_candidates(
    recipe_id: str, nutrition_source: NutritionSource, candidate_refs: Sequence[CompositionRef] | None = None, *,
    baseline_run_id: str, quantity_input_hash: str, title: str, instructions: Sequence[str],
    target_position: int, objective: NutrientObjective, constraints: ComparisonConstraints | None = None,
    quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
) -> AdaptationPreview:
    """Read-only preview; omitted refs invoke the compatible catalogue shortlist."""
    if candidate_refs is not None:
        _check_pool(candidate_refs)
    failed = AdaptationPreview(recipe_id=recipe_id, baseline_run_id=baseline_run_id,
        quantity_input_hash=quantity_input_hash, target_position=target_position, objective=objective.model_copy(deep=True),
        status="needs_evidence")
    try:
        snapshot = load_active_snapshot(recipe_id, nutrition_source)
        if snapshot.run_id != baseline_run_id or snapshot.quantity_input_hash != quantity_input_hash:
            raise ComparisonLoadError("baseline_mismatch", blocked=True)
        selection = None
        if candidate_refs is None:
            from .candidate_selection import shortlist_raw_part_candidates
            selection = shortlist_raw_part_candidates(snapshot, target_position=target_position,
                objective=objective, constraints=constraints, quantity_declarations=quantity_declarations)
            candidate_refs = selection.candidate_refs
        loaded, unavailable = [], []
        for ref in candidate_refs:
            try:
                if ref.release_id != snapshot.release_id or ref.collection not in {
                    "nutritional_ingredients_eu", "nutritional_ingredients_" + nutrition_source,
                }:
                    raise ComparisonLoadError("candidate_release_or_region_mismatch", blocked=True)
                rows = matching_store.composition_records(ref.release_id, ref.collection, candidate_id=ref.candidate_id)
                if not rows:
                    raise ComparisonLoadError("candidate_composition_not_found")
                composition = _composition(rows[0])
                if composition.ref != ref:
                    raise ComparisonLoadError("candidate_reference_mismatch", blocked=True)
                loaded.append(composition)
            except ComparisonLoadError as error:
                unavailable.append(PreviewCandidate(candidate_id=_candidate_id(ref), composition=ref.model_copy(deep=True),
                    label=ref.candidate_id, status="rejected" if error.blocked else "needs_evidence", reasons=[error.reason]))
        result = preview_raw_part_candidates(snapshot, loaded, title=title, instructions=instructions,
            target_position=target_position, objective=objective, constraints=constraints,
            quantity_declarations=quantity_declarations)
        result.candidates.extend(unavailable)
        result.candidate_selection = selection
        current = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
        if current is None or current["run_id"] != snapshot.run_id:
            result.status, result.reasons = "blocked", ["active_profile_changed"]
            for item in result.candidates:
                item.rank = None
                if item.status != "rejected":
                    item.status = "needs_evidence"
                    item.reasons.append("active_profile_changed")
            return result
        return _summarize(result)
    except ComparisonLoadError as error:
        failed.status = "blocked" if error.blocked else "needs_evidence"
        failed.reasons = [error.reason]
        return failed
    except (ValidationError, KeyError, TypeError):
        failed.status, failed.reasons = "blocked", ["malformed_matching_record"]
        return failed
