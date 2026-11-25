"""Exact draft edits cannot expand a validated single-ingredient operation."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import openai
import pytest

from test_adaptation_preview import inputs, model, run_preview

from recipe_wrangler.services.adaptation import instruction_editor as editor
from recipe_wrangler.services.adaptation import contextual_assessment as culinary
from recipe_wrangler.services.adaptation.models import QuantityBasisDeclaration
from recipe_wrangler.services.adaptation.models import InstructionPatchPlan
from recipe_wrangler.services.adaptation.preview import preview_raw_part_candidates
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.services.adaptation.models import OccurrenceSourceEvidence, RawPartEdit


@pytest.fixture
def setup(inputs, model, monkeypatch):
    snapshot = inputs[0]
    preview = run_preview(inputs)
    calls = []
    response = {}
    monkeypatch.setattr(editor, "_llm_config", lambda: {"source": "vllm", "model": "mock-editor"})
    def request(payload, response_model, config, **kwargs):
        calls.append((deepcopy(payload), kwargs))
        if "error" in response:
            raise response["error"]
        if "raw" in response:
            return response["raw"]
        plan = {"status": "proposed", "patches": deepcopy(payload["allowed_patches"]), "uncertainties": []}
        if "mutate" in response:
            response["mutate"](plan)
        return json.dumps(plan)
    monkeypatch.setattr(editor, "_request", request)
    return snapshot, preview, calls, response


def draft(setup, **kwargs):
    snapshot, preview, calls, response = setup
    return editor.draft_raw_part_instructions(snapshot, preview, kwargs.pop("candidate_id", preview.candidates[0].candidate_id),
        title=kwargs.pop("title", "Chicken dish"),
        instructions=kwargs.pop("instructions", ["Cook the chicken thigh gently."]), **kwargs)


def test_supported_edit_produces_reviewable_draft_and_preserves_baseline(setup):
    snapshot, preview, calls, response = setup
    baseline = deepcopy((snapshot.model_dump(), preview.model_dump()))
    result = draft(setup)
    assert result.status == "draft_requires_review" and result.recipe_ready is False
    assert result.title == "Chicken dish"
    assert result.ingredients[0].name == "chicken breast"
    assert result.ingredients[0].original_text == "200 g raw boneless skinless chicken breast"
    assert result.ingredients[0].quantity == snapshot.ingredients[0].quantity
    assert result.instructions[0].text == "Cook the chicken breast gently."
    assert result.comparison == preview.candidates[0].comparison
    assert len(calls) == 1 and calls[0][1]["system_prompt"] == editor.EDITOR_PROMPT
    assert result.model == "mock-editor" and result.context_hash and result.preview_hash
    assert result.evidence_status == "inferred" and result.uncertainties
    assert (snapshot.model_dump(), preview.model_dump()) == baseline
    result.model_dump_json()


def test_unselected_option_is_removed_from_draft_text_and_stock_is_preserved(inputs):
    snapshot, targets = inputs
    item = snapshot.ingredients[0]
    item.name = "chicken thighs"
    line = "  8 chicken thighs, skin and bone removed (or 4 chicken legs, skin removed)"
    item.original_text = line
    item.quantity.evidence.status = "proxy"
    item.source_evidence = OccurrenceSourceEvidence(recipe_id=snapshot.recipe_id,
        baseline_run_id=snapshot.run_id, quantity_input_hash=snapshot.quantity_input_hash,
        position=0, weight_g=item.quantity.weight_g, source_line=line, source_hash=digest(line),
        selected_source_text="8 chicken thighs, skin and bone removed")
    stock = item.model_copy(deep=True)
    stock.position = 1
    stock.name = "chicken stock"
    stock.original_text = "850ml chicken stock"
    stock.source_evidence = None
    stock.observed_properties = {"base_identity": item.observed_properties["base_identity"].model_copy(update={"value": "chicken stock"})}
    snapshot.ingredients.append(stock)
    steps = ["Brown the chicken thighs.", "Add the chicken stock.", "Cook gently for one hour."]
    context = culinary.build_adaptation_context(snapshot, title="Chicken casserole", instructions=steps,
        objective="Reduce saturated fat", target_position=0)
    before = context.model_dump()
    edit = RawPartEdit(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        source_identity="chicken", target_identity="chicken", source_biological_source="chicken",
        target_biological_source="chicken", source_food_part="thigh", target_food_part="breast",
        quantity_input_hash=snapshot.quantity_input_hash, position=0,
        from_composition=item.composition.ref, to_composition=targets[0].ref)
    patches, texts = editor._allowed_patches(context, edit)
    for patch in sorted(patches, key=lambda p: p.start, reverse=True):
        key = (patch.kind, patch.reference_id)
        assert texts[key][patch.start:patch.end] == patch.before
        texts[key] = texts[key][:patch.start] + patch.after + texts[key][patch.end:]
    assert texts[("ingredient_text", "ingredient:0")] == "  approximately 200 g chicken breasts, skin and bone removed"
    assert texts[("instruction", "step:0")] == "Brown the chicken breasts."
    assert texts[("instruction", "step:1")] == steps[1]
    assert texts[("instruction", "step:2")] == steps[2]
    assert context.model_dump() == before
    # A stock line also containing meat still makes generic chicken references ambiguous.
    context.ingredients[1].original_text += " with chicken thighs"
    with pytest.raises(ValueError, match="ambiguous_substitution_occurrences"):
        editor._allowed_patches(context, edit)


def test_instruction_selection_rejects_an_unselected_food(inputs):
    snapshot, _ = inputs
    item = snapshot.ingredients[0]
    item.original_text = "8 chicken thighs (or 4 chicken breasts)"
    item.source_evidence = OccurrenceSourceEvidence(recipe_id=snapshot.recipe_id,
        baseline_run_id=snapshot.run_id, quantity_input_hash=snapshot.quantity_input_hash,
        position=0, weight_g=item.quantity.weight_g, source_line=item.original_text,
        source_hash=digest(item.original_text), selected_source_text="4 chicken breasts")
    context = culinary.build_adaptation_context(snapshot, title="Chicken casserole",
        instructions=["Brown the chicken thighs."], objective="Reduce saturated fat", target_position=0)
    with pytest.raises(ValueError, match="source_alternative_selection_mismatch"):
        editor._allowed_patches(context)


@pytest.mark.parametrize("issue", ["run", "hash", "quantity", "serves", "observed", "source_values", "title", "steps", "objective"])
def test_changed_baseline_or_objective_abstains_before_inference(setup, issue):
    snapshot, preview, calls, response = setup
    kwargs = {}
    if issue == "run": snapshot.run_id = "new-run"
    elif issue == "hash": snapshot.quantity_input_hash = "new-hash"
    elif issue == "quantity": snapshot.ingredients[0].quantity.weight_g = 210
    elif issue == "serves": snapshot.serves = 3
    elif issue == "observed": snapshot.ingredients[0].observed_properties["skin_status"].value = "skin_on"
    elif issue == "source_values": snapshot.ingredients[0].composition.values_per_100g["energy_kcal"] = 250
    elif issue == "title": kwargs["title"] = "Changed title"
    elif issue == "steps": kwargs["instructions"] = ["Cook the chicken thigh for 15 minutes."]
    else: preview.objective = NutrientObjective(metric="fat_g")
    result = draft(setup, **kwargs)
    assert result.status == "needs_evidence" and not calls
    assert not result.instructions and not result.ingredients and not result.patches


@pytest.mark.parametrize("issue", ["blocked_preview", "conditional", "unknown_candidate", "altered_numeric", "altered_roles", "altered_culinary", "reference", "gain"])
def test_ineligible_or_inconsistent_preview_cannot_be_edited(setup, issue):
    snapshot, preview, calls, response = setup
    candidate = preview.candidates[0]
    kwargs = {}
    if issue == "blocked_preview": preview.status = "blocked"
    elif issue == "conditional": candidate.status = "needs_changes"
    elif issue == "unknown_candidate": kwargs["candidate_id"] = "not-selected"
    elif issue == "altered_numeric": candidate.comparison.recipe_delta_per_serving["energy_kcal"] = -1000
    elif issue == "altered_roles": preview.roles.assessment.roles[0].reason = "Changed evidence"
    elif issue == "altered_culinary": candidate.culinary.reason = "Changed assessment"
    elif issue == "reference": candidate.composition.candidate_id = "other"
    else: candidate.objective_improvement_per_serving = 1000
    assert draft(setup, **kwargs).status == "needs_evidence" and not calls


@pytest.mark.parametrize("mutation", ["extra", "omit", "duplicate", "offset", "before", "after", "step", "quantity", "new_field"])
def test_model_cannot_expand_change_or_return_partial_patch_set(setup, mutation):
    response = setup[3]
    def mutate(plan):
        patch = plan["patches"][0]
        if mutation == "extra": plan["patches"].append({**patch, "before": "gently", "after": "boil"})
        elif mutation == "omit": plan["patches"].pop()
        elif mutation == "duplicate": plan["patches"].append(deepcopy(patch))
        elif mutation == "offset": patch["start"] += 1
        elif mutation == "before": patch["before"] = "chicken thigh"
        elif mutation == "after": patch["after"] = "breast in added oil"
        elif mutation == "step": patch["reference_id"] = "step:99"
        elif mutation == "quantity": patch.update(before="200", after="250")
        else: plan["approved"] = True
    response["mutate"] = mutate
    result = draft(setup)
    assert result.status == "unavailable" and not result.instructions and not result.patches


@pytest.mark.parametrize("raw", ['{"status":"proposed"}', '{"status":"uncertain","status":"proposed"}',
                                  '```json\n{}\n```', 'not JSON'])
def test_malformed_or_duplicate_json_never_edits(setup, raw):
    setup[3]["raw"] = raw
    assert draft(setup).status == "unavailable"


def test_provider_failure_preserves_abstention(setup):
    setup[3]["error"] = openai.APIConnectionError(request=None)
    result = draft(setup)
    assert result.status == "unavailable" and not result.instructions


def test_editor_uncertainty_remains_visible_without_partial_edits(setup):
    setup[3]["raw"] = json.dumps({"status": "uncertain", "patches": [], "uncertainties": ["Cooking suitability needs review."]})
    result = draft(setup)
    assert result.status == "needs_evidence" and result.uncertainties == ["Cooking suitability needs review."]
    assert not result.instructions and not result.patches


def test_plural_case_multiple_spans_and_other_steps_are_preserved(inputs, model, setup):
    snapshot = inputs[0]
    snapshot.ingredients[0].name = "CHICKEN THIGHS"
    snapshot.ingredients[0].original_text = "200 g raw boneless skinless chicken thighs"
    steps = ["Cook the chicken thigh gently.",
             "Turn the chicken thighs after 5 minutes at 180 C. Turn the chicken thighs again.", "Serve with water."]
    preview = preview_raw_part_candidates(snapshot, inputs[1], title="Chicken thighs", instructions=steps,
        target_position=0, objective=NutrientObjective(metric="energy_kcal"))
    assert preview.status == "has_supported_candidates"
    result = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken thighs", instructions=steps)
    assert result.status == "draft_requires_review"
    assert result.title == "Chicken breasts" and result.ingredients[0].name == "CHICKEN BREASTS"
    assert result.instructions[1].text == "Turn the chicken breasts after 5 minutes at 180 C. Turn the chicken breasts again."
    assert result.instructions[2].text == steps[2]


@pytest.mark.parametrize("step,reason", [("Cook the thighs gently.", "unresolved_part_reference"),
                                         ("Cook it gently.", "explicit_substitution_reference_required")])
def test_implicit_references_require_broader_linking(inputs, model, setup, monkeypatch, step, reason):
    request = culinary._request
    def cited_request(payload, response_model, config, **kwargs):
        value = json.loads(request(payload, response_model, config, **kwargs))
        items = value.get("roles", value.get("assessments", []))
        for item in items:
            for quote in item["evidence"]:
                if quote["kind"] == "instruction": quote["quote"] = step
        return json.dumps(value)
    monkeypatch.setattr(culinary, "_request", cited_request)
    steps = [step]
    preview = preview_raw_part_candidates(inputs[0], inputs[1], title="Chicken dish", instructions=steps,
        target_position=0, objective=NutrientObjective(metric="energy_kcal"))
    result = editor.draft_raw_part_instructions(inputs[0], preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=steps)
    assert result.status == "needs_evidence" and reason in result.reasons and not setup[2]


def test_multiple_chicken_occurrences_need_explicit_step_links(inputs, model, setup):
    snapshot = inputs[0]
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position, other.name, other.original_text = 1, "chicken breast", "100 g chicken breast"
    other.quantity.weight_g = 100
    snapshot.ingredients.append(other)
    preview = run_preview(inputs)
    assert preview.status == "has_supported_candidates"
    result = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])
    assert result.reasons == ["ambiguous_substitution_occurrences"] and not setup[2]


def test_quantity_attestation_remains_separate_from_display_and_observed_facts(inputs, model, setup):
    snapshot = inputs[0]
    snapshot.ingredients[0].quantity.basis = "unknown"
    declaration = QuantityBasisDeclaration.model_validate({
        "recipe_id": snapshot.recipe_id, "baseline_run_id": snapshot.run_id,
        "quantity_input_hash": snapshot.quantity_input_hash, "position": 0, "weight_g": 200,
        "basis": "raw_edible", "declared_by": "test-reviewer", "statement": "Source states raw edible mass.",
        "evidence": {"reference": "test-source", "method": "source_review", "version": "1", "status": "stated"},
    })
    preview = run_preview(inputs, quantity_declarations=[declaration])
    result = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])
    assert result.status == "draft_requires_review"
    assert result.ingredients[0].quantity.basis == "unknown"
    assert result.comparison.quantity.basis == "raw_edible"
    assert result.comparison.quantity_declarations == [declaration]
    assert snapshot.ingredients[0].observed_properties["food_part"].value == "thigh"


def test_unrelated_ingredient_and_quantity_are_preserved(inputs, model, setup):
    snapshot = inputs[0]
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position, other.name, other.original_text = 1, "rice", "100 g raw rice"
    other.quantity.weight_g = 100
    other.observed_properties = {}
    other.composition.properties["base_identity"].value = "rice"
    other.composition.properties["biological_source"].value = "rice"
    snapshot.ingredients.append(other)
    preview = run_preview(inputs)
    result = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])
    assert result.status == "draft_requires_review"
    assert result.ingredients[1].name == other.name and result.ingredients[1].original_text == other.original_text
    assert result.ingredients[1].quantity == other.quantity
    assert all(item.reference_id != "ingredient:1" for item in result.patches)


@pytest.mark.parametrize("custom_prompt", [False, True])
def test_shared_transport_uses_editor_prompt_only_when_requested(monkeypatch, custom_prompt):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{}'))])
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr(culinary.openai, "OpenAI", factory)
    config = {"source": "vllm", "model": "mock-editor", "base_url": "http://test.invalid/v1", "api_key": "test", "timeout": 1}
    kwargs = {"system_prompt": editor.EDITOR_PROMPT} if custom_prompt else {}
    assert culinary._request({"task": "test"}, InstructionPatchPlan, config, max_tokens=100, **kwargs) == '{}'
    sent = client.chat.completions.create.call_args.kwargs
    assert sent["messages"][0]["content"] == (editor.EDITOR_PROMPT if custom_prompt else culinary.SYSTEM_PROMPT)
    assert sent["response_format"] == {"type": "json_object"}
    assert json.loads(sent["messages"][1]["content"])["response_schema"] == InstructionPatchPlan.model_json_schema()
    assert factory.call_args.kwargs["max_retries"] == 0
    factory.return_value.__exit__.assert_called_once()
