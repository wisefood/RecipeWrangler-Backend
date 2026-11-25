"""Assemble checked preview and instruction outputs into one recipe dictionary."""

from collections.abc import Sequence

from recipe_wrangler.repositories import matching_store

from .comparison import compare_raw_part
from .models import (
    ComparisonConstraints, ComparisonSnapshot, CompositionRef, NutritionSource, QuantityBasisDeclaration,
)
from .comparison_store import ComparisonLoadError, load_active_snapshot
from .contextual_assessment import _context_payload, build_adaptation_context
from .instruction_editor import _allowed_patches, draft_raw_part_instructions
from .models import InstructionDraft
from .cooking_parameters import validate_cooking_adjustments
from .preview import preview_active_raw_part_candidates
from .models import AdaptationPreview, NutrientObjective
from .quantity_declarations import apply_quantity_declarations
from .comparison import restriction_text_concerns
from .models import (
    AdaptedRecipe, AdaptedRecipeIngredient, RecipeAdaptationResponse, RecipeIngredientChange, RecipeNutrition,
)


def _empty_response(preview: AdaptationPreview) -> RecipeAdaptationResponse:
    result = RecipeAdaptationResponse(status="instruction_pending" if preview.status == "has_supported_candidates" else preview.status,
        original_recipe_id=preview.recipe_id, baseline_run_id=preview.baseline_run_id,
        quantity_input_hash=preview.quantity_input_hash, objective=preview.objective.model_copy(deep=True),
        constraints=preview.context.constraints.model_copy(deep=True) if preview.context else ComparisonConstraints(),
        reasons=list(preview.reasons))
    diagnostics = {}
    if preview.candidate_selection:
        result.reasons.extend(preview.candidate_selection.reasons)
        for item in preview.candidate_selection.screening:
            key = (item.composition.release_id, item.composition.collection, item.composition.candidate_id)
            diagnostics[key] = item.model_dump(mode="json", exclude={"comparison"})
    for item in preview.candidates:
        key = (item.composition.release_id, item.composition.collection, item.composition.candidate_id)
        diagnostics[key] = item.model_dump(mode="json", exclude={"comparison"})
        if item.culinary:
            result.uncertainties.extend(item.culinary.uncertainties)
    result.candidate_diagnostics = list(diagnostics.values())
    if preview.roles and preview.roles.assessment:
        result.uncertainties.extend(preview.roles.assessment.uncertainties)
    for assessment in (preview.roles, preview.culinary_batch):
        if assessment and assessment.failure_reason:
            result.reasons.append(assessment.failure_reason)
    result.reasons = list(dict.fromkeys(result.reasons))
    result.uncertainties = list(dict.fromkeys(result.uncertainties))
    return result


def assemble_adaptation_response(
    snapshot: ComparisonSnapshot, preview: AdaptationPreview, draft: InstructionDraft | None = None,
) -> RecipeAdaptationResponse:
    """Join trusted internal results; recheck calculations and exact patched text.

    This performs no inference, persistence, cooked-nutrition calculation or
    Nutri-Score optimization. Hashes pin content, not public authorization.
    """
    snapshot = ComparisonSnapshot.model_validate(snapshot.model_dump())
    preview = AdaptationPreview.model_validate(preview.model_dump())
    result = _empty_response(preview)
    if (snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash) != (
        preview.recipe_id, preview.baseline_run_id, preview.quantity_input_hash
    ):
        result.status, result.reasons = "blocked", ["baseline_mismatch"]
        return result
    if preview.status != "has_supported_candidates" or draft is None:
        return result
    try:
        draft = InstructionDraft.model_validate(draft.model_dump())
        if draft.status != "draft_requires_review":
            result.status, result.reasons = draft.status, list(draft.reasons)
            result.uncertainties = list(draft.uncertainties)
            return result
        context = preview.context
        if context is None or (draft.recipe_id, draft.baseline_run_id, draft.quantity_input_hash) != (
            snapshot.recipe_id, snapshot.run_id, snapshot.quantity_input_hash
        ):
            raise ValueError("draft_scope_mismatch")
        payload = _context_payload(context)
        packet = preview.model_dump(mode="json")
        packet["context"] = payload
        if draft.context_hash != matching_store.digest(payload) or draft.preview_hash != matching_store.digest(packet):
            raise ValueError("draft_preview_mismatch")
        expected = build_adaptation_context(snapshot, title=context.title, instructions=[], objective=context.objective,
            target_position=preview.target_position, constraints=context.constraints)
        if expected.ingredients != context.ingredients:
            raise ValueError("baseline_ingredient_context_mismatch")
        selected = [item for item in preview.candidates if item.candidate_id == draft.candidate_id]
        if len(selected) != 1 or selected[0].status != "ready_for_instruction_editing":
            raise ValueError("candidate_not_ready")
        candidate = selected[0]
        comparison = candidate.comparison
        if comparison is None or comparison.target_composition is None or draft.comparison != comparison:
            raise ValueError("draft_comparison_mismatch")
        checked = compare_raw_part(snapshot, comparison.edit, comparison.target_composition,
            context.constraints, quantity_declarations=comparison.quantity_declarations)
        if checked.status != "supported_input_comparison" or checked.model_dump() != comparison.model_dump():
            raise ValueError("numerical_comparison_changed")
        allowed, texts = _allowed_patches(context, checked.edit)
        allowed += validate_cooking_adjustments(context, candidate.culinary, draft.cooking_adjustments)
        if sorted(item.model_dump_json() for item in draft.patches) != sorted(item.model_dump_json() for item in allowed):
            raise ValueError("draft_patch_mismatch")
        for patch in sorted(allowed, key=lambda item: item.start, reverse=True):
            key = (patch.kind, patch.reference_id)
            text = texts[key]
            if text[patch.start:patch.end] != patch.before:
                raise ValueError("draft_patch_source_mismatch")
            texts[key] = text[:patch.start] + patch.after + text[patch.end:]
        if draft.title != texts[("title", "recipe:title")]:
            raise ValueError("draft_title_changed")
        if [(item.step_id, item.text) for item in draft.instructions] != [
            (item.step_id, texts[("instruction", item.step_id)]) for item in context.steps
        ]:
            raise ValueError("draft_instructions_changed")
        recipe_texts = [draft.title or "", *[step.text for step in draft.instructions],
            *[item.original_text or item.name for item in draft.ingredients]]
        concerns = [reason for text in recipe_texts for reason in restriction_text_concerns(text, context.constraints)]
        if concerns:
            raise ValueError("unresolved_recipe_text_restriction_evidence:" + ",".join(sorted(set(concerns))))
        if [item.position for item in draft.ingredients] != [item.position for item in snapshot.ingredients]:
            raise ValueError("draft_ingredient_partition_mismatch")
        effective, _ = apply_quantity_declarations(snapshot, comparison.quantity_declarations)
        saved_details = {item['position']: item for item in
            snapshot.provenance.get('active_run', {}).get('profile', {}).get('details', [])}
        ingredients = []
        for original, proposed, effective_item, context_item in zip(
            snapshot.ingredients, draft.ingredients, effective.ingredients, context.ingredients, strict=True
        ):
            replaced = original.position == preview.target_position
            name = texts[("ingredient_name", f"ingredient:{original.position}")] if replaced else context_item.name
            original_text = texts[("ingredient_text", f"ingredient:{original.position}")] if replaced else context_item.original_text
            if (proposed.name, proposed.original_text, proposed.quantity) != (name, original_text, original.quantity):
                raise ValueError("draft_ingredient_changed")
            composition = comparison.target_composition if replaced else original.composition
            ingredients.append(AdaptedRecipeIngredient(position=original.position, name=name, original_text=original_text,
                quantity=effective_item.quantity.model_copy(deep=True), baseline_quantity=original.quantity.model_copy(deep=True),
                composition=composition.ref.model_copy(deep=True) if composition else None,
                matched_label=(composition.provenance.get('candidate') or {}).get('document') if composition else None,
                quantity_assumption=saved_details.get(original.position, {}).get('quantity_assumption'),
                baseline_observed_properties={key: value.model_copy(deep=True) for key, value in original.observed_properties.items()},
                proposed_composition_properties={key: value.model_copy(deep=True) for key, value in composition.properties.items()} if composition else {}))
        after = checked.after
        nutrition = RecipeNutrition(basis=checked.quantity_basis,
            totals=after["complete_totals"], per_serving=after["complete_totals_per_serving"],
            supported_partial_totals=after["supported_partial_totals"],
            supported_partial_per_serving=after["supported_partial_totals_per_serving"],
            completeness={key: after[key] for key in ("nutrition_status", "nutrient_status", "missing_contributions", "missing_mass_share")})
        nutrition.completeness['ingredient_gaps'] = [{**gap,
            'matched': next(item.composition is not None for item in ingredients if item.position == gap['position'])}
            for gap in after.get('ingredient_gaps', [])]
        if after['nutrition_status'] == 'minor_gaps':
            result.uncertainties.append("Nutrition estimates omit small missing contributions; see ingredient gaps and missing mass shares. Unmatched ingredients remain on the card.")
        if any(item.quantity_assumption for item in ingredients):
            result.uncertainties.append("Unspecified salt/pepper amounts use the disclosed 1 g per ingredient per recipe assumption, including in sodium estimates.")
        result.adapted_recipe = AdaptedRecipe(title=draft.title, serves=snapshot.serves, ingredients=ingredients,
            instructions=[item.text for item in draft.instructions], nutrition=nutrition)
        original = next(item for item in snapshot.ingredients if item.position == preview.target_position)
        proposed = next(item for item in draft.ingredients if item.position == preview.target_position)
        result.changes = [RecipeIngredientChange(from_ingredient=original.name, to_ingredient=proposed.name,
                                                 edit=checked.edit.model_copy(deep=True))]
        result.comparison = checked.model_copy(deep=True)
        result.selected_candidate_id = draft.candidate_id
        result.constraint_status = "passed_input_comparison"
        result.provenance = {"catalogue_release": snapshot.release_id, "nutrition_source": snapshot.nutrition_source,
            "context_hash": draft.context_hash, "preview_hash": draft.preview_hash,
            "instruction_patches": [item.model_dump(mode="json") for item in draft.patches],
            "cooking_adjustments": [item.model_dump(mode="json") for item in draft.cooking_adjustments],
            "quantity_declarations": [item.model_dump(mode="json") for item in checked.quantity_declarations],
            "source_evidence": [item.source_evidence.model_dump(mode="json") for item in snapshot.ingredients if item.source_evidence],
            "models": {"roles": {"model": preview.roles.model, "source": preview.roles.source, "prompt_version": preview.roles.prompt_version},
                "candidates": {"model": preview.culinary_batch.model, "source": preview.culinary_batch.source,
                               "prompt_version": preview.culinary_batch.prompt_version},
                "editor": {"model": draft.model, "source": draft.source, "version": draft.editor_version}}}
        result.uncertainties = list(dict.fromkeys(result.uncertainties + checked.uncertainty + draft.uncertainties))
        result.status, result.reasons = "draft_requires_review", []
        return RecipeAdaptationResponse.model_validate(result.model_dump())
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        failed = _empty_response(preview)
        failed.status, failed.reasons = "blocked", [str(error)]
        return failed


def adapt_active_raw_part_recipe(
    recipe_id: str, nutrition_source: NutritionSource, *, baseline_run_id: str, quantity_input_hash: str,
    title: str, instructions: Sequence[str], target_position: int, objective: NutrientObjective,
    candidate_refs: Sequence[CompositionRef] | None = None, candidate_id: str | None = None,
    constraints: ComparisonConstraints | None = None, quantity_declarations: Sequence[QuantityBasisDeclaration] = (),
) -> dict:
    """Return one recipe response; caller supplies authoritative text. Never saves."""
    preview = preview_active_raw_part_candidates(recipe_id, nutrition_source, candidate_refs,
        baseline_run_id=baseline_run_id, quantity_input_hash=quantity_input_hash, title=title,
        instructions=instructions, target_position=target_position, objective=objective,
        constraints=constraints, quantity_declarations=quantity_declarations)
    if preview.status != "has_supported_candidates":
        result = _empty_response(preview)
        if preview.context is None and constraints is not None:
            result.constraints = constraints.model_copy(deep=True)
        return result.model_dump(mode="json")
    ready = sorted((item for item in preview.candidates if item.status == "ready_for_instruction_editing" and item.rank is not None),
                   key=lambda item: item.rank)
    selected = next((item for item in ready if item.candidate_id == candidate_id), None) if candidate_id else next(iter(ready), None)
    if selected is None:
        result = _empty_response(preview)
        result.status, result.reasons = "blocked", ["candidate_not_ready_or_missing"]
        return result.model_dump(mode="json")
    try:
        snapshot = load_active_snapshot(recipe_id, nutrition_source)
        draft = draft_raw_part_instructions(snapshot, preview, selected.candidate_id, title=title, instructions=instructions)
        result = assemble_adaptation_response(snapshot, preview, draft)
        current = matching_store.active_profile(recipe_id, nutrition_source, include_provenance=True)
        if current is None or current["run_id"] != snapshot.run_id:
            result = _empty_response(preview)
            result.status, result.reasons = "blocked", ["active_profile_changed"]
    except ComparisonLoadError as error:
        result = _empty_response(preview)
        result.status = "blocked" if error.blocked else "needs_evidence"
        result.reasons = [error.reason]
    return result.model_dump(mode="json")
