"""One recipe response cannot mix drafts, baselines, quantities or nutrients."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from test_adaptation_instruction_editor import setup
from test_adaptation_preview import inputs, model, run_preview

from recipe_wrangler.repositories import matching_store
from recipe_wrangler.services.adaptation import instruction_editor as editor
from recipe_wrangler.services.adaptation import recipe_response as service
from recipe_wrangler.services.adaptation.models import ComparisonConstraints, QuantityBasisDeclaration
from recipe_wrangler.services.adaptation.models import PropertyAssertion
from recipe_wrangler.services.adaptation.models import RecipeAdaptationResponse


def make_draft(setup):
    snapshot, preview, calls, response = setup
    return editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])


def test_complete_response_has_edited_recipe_computed_nutrition_and_traceable_changes(setup):
    snapshot, preview, calls, response = setup
    draft = make_draft(setup)
    before = deepcopy((snapshot.model_dump(), preview.model_dump(), draft.model_dump()))
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    output = result.model_dump(mode="json")
    recipe = output["adapted_recipe"]
    assert result.status == "draft_requires_review" and not output["recipe_ready"] and not output["saved"]
    assert recipe["title"] == "Chicken dish" and recipe["serves"] == 2
    assert recipe["ingredients"][0]["name"] == "chicken breast"
    assert recipe["ingredients"][0]["quantity"]["weight_g"] == 200
    assert recipe["ingredients"][0]["baseline_observed_properties"]["food_part"]["value"] == "thigh"
    assert recipe["ingredients"][0]["proposed_composition_properties"]["food_part"]["value"] == "breast"
    assert recipe["instructions"] == ["Cook the chicken breast gently."]
    assert recipe["nutrition"]["totals"]["energy_kcal"] == 280
    assert recipe["nutrition"]["per_serving"]["energy_kcal"] == 140
    assert recipe["nutrition"]["basis"] == "raw_edible_input"
    assert recipe["nutrition"]["nutri_score"] is None and recipe["nutrition"]["nutri_score_status"] == "not_calculated"
    assert output["comparison"]["recipe_delta_per_serving"]["energy_kcal"] == -60
    assert output["changes"][0]["from_ingredient"] == "chicken thigh"
    assert output["changes"][0]["to_ingredient"] == "chicken breast"
    assert output["constraint_status"] == "passed_input_comparison"
    assert output["provenance"]["context_hash"] and output["provenance"]["models"]["editor"]["model"] == "mock-editor"
    assert (snapshot.model_dump(), preview.model_dump(), draft.model_dump()) == before
    json.dumps(output, allow_nan=False)


@pytest.mark.parametrize("location", ["title", "ingredient", "instruction"])
def test_final_recipe_text_cannot_hide_milk_behind_composition_absence(inputs, model, setup, location):
    snapshot, candidates = inputs
    for composition in [snapshot.ingredients[0].composition, *candidates]:
        composition.allergen_assessments["milk"] = PropertyAssertion(
            value="absent", evidence=composition.basis.evidence)
    title, instructions = "Chicken dish", ["Cook the chicken thigh gently."]
    if location == "title": title += " with cream"
    elif location == "ingredient": snapshot.ingredients[0].original_text += " with cream"
    else: instructions[0] += " Serve with cream."
    from recipe_wrangler.services.adaptation.preview import preview_raw_part_candidates
    from recipe_wrangler.services.adaptation.models import NutrientObjective
    preview = preview_raw_part_candidates(snapshot, candidates, title=title, instructions=instructions,
        target_position=0, objective=NutrientObjective(metric="energy_kcal"),
        constraints=ComparisonConstraints(forbidden_allergens={"milk"}))
    draft = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title=title, instructions=instructions)
    assert draft.status == "draft_requires_review", draft.reasons
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    assert result.status == "blocked" and result.adapted_recipe is None
    assert result.reasons == ["unresolved_recipe_text_restriction_evidence:allergen:milk"]


@pytest.mark.parametrize("mutation", ["title", "instruction", "mass", "ingredient_name", "ingredient_text",
    "ingredient_position", "missing_ingredient", "patch", "comparison", "context_hash", "preview_hash", "run"])
def test_tampered_or_mismatched_draft_never_returns_recipe(setup, mutation):
    snapshot, preview, calls, response = setup
    draft = make_draft(setup)
    if mutation == "title": draft.title = "Different dish"
    elif mutation == "instruction": draft.instructions[0].text += " Add butter."
    elif mutation == "mass": draft.ingredients[0].quantity.weight_g = 250
    elif mutation == "ingredient_name": draft.ingredients[0].name = "turkey"
    elif mutation == "ingredient_text": draft.ingredients[0].original_text = "250 g chicken breast"
    elif mutation == "ingredient_position": draft.ingredients[0].position = 1
    elif mutation == "missing_ingredient": draft.ingredients.clear()
    elif mutation == "patch": draft.patches.pop()
    elif mutation == "comparison": draft.comparison.after["complete_totals"]["energy_kcal"] = 1
    elif mutation == "context_hash": draft.context_hash = "changed"
    elif mutation == "preview_hash": draft.preview_hash = "changed"
    else: draft.baseline_run_id = "other-run"
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    assert result.status == "blocked" and result.adapted_recipe is None and not result.changes


@pytest.mark.parametrize("mutation", ["serves", "weight", "nutrient", "run"])
def test_changed_baseline_never_receives_old_nutrition(setup, mutation):
    snapshot, preview, calls, response = setup
    draft = make_draft(setup)
    if mutation == "serves": snapshot.serves = 4
    elif mutation == "weight": snapshot.ingredients[0].quantity.weight_g = 250
    elif mutation == "nutrient": snapshot.ingredients[0].composition.values_per_100g["energy_kcal"] = 300
    else: snapshot.run_id = "other"
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    assert result.status == "blocked" and result.adapted_recipe is None


def test_preview_without_draft_is_pending_not_a_completed_recipe(setup):
    result = service.assemble_adaptation_response(setup[0], setup[1])
    assert result.status == "instruction_pending" and result.adapted_recipe is None


def test_unknown_nonobjective_nutrients_remain_unknown(inputs, model, setup):
    for item in inputs[1]: item.values_per_100g["sugar_g"] = None
    preview = run_preview(inputs)
    draft = editor.draft_raw_part_instructions(inputs[0], preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])
    result = service.assemble_adaptation_response(inputs[0], preview, draft)
    nutrition = result.adapted_recipe.nutrition
    assert nutrition.totals["sugar_g"] is None and nutrition.per_serving["sugar_g"] is None
    assert nutrition.supported_partial_totals["sugar_g"] == 0
    assert nutrition.completeness["nutrient_status"]["sugar_g"] == "incomplete"


def test_unmatched_pinch_stays_on_complete_card_with_disclosed_nutrition_gap(inputs, model, setup):
    from recipe_wrangler.services.adaptation.models import ComparisonIngredient
    snapshot = inputs[0]
    quantity = snapshot.ingredients[0].quantity.model_copy(update={'weight_g': 0.2})
    snapshot.ingredients.append(ComparisonIngredient(position=1, name='dried chilli flakes',
        original_text='pinch of dried chilli flakes', quantity=quantity, observed_properties={}, composition=None))
    preview = run_preview(inputs)
    draft = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title='Chicken dish', instructions=['Cook the chicken thigh gently.'])
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    assert result.status == 'draft_requires_review', result.reasons
    assert result.adapted_recipe.ingredients[1].composition is None
    assert result.adapted_recipe.ingredients[1].original_text == 'pinch of dried chilli flakes'
    nutrition = result.adapted_recipe.nutrition
    assert nutrition.completeness['nutrition_status'] == 'minor_gaps'
    assert nutrition.completeness['ingredient_gaps'][0]['name'] == 'dried chilli flakes'
    assert nutrition.completeness['ingredient_gaps'][0]['matched'] is False
    assert nutrition.completeness['missing_mass_share']['energy_kcal'] > 0
    assert any('omit small missing contributions' in warning for warning in result.uncertainties)


def test_declared_quantity_basis_remains_distinct_from_baseline(inputs, model, setup):
    snapshot = inputs[0]
    snapshot.ingredients[0].quantity.basis = "unknown"
    declaration = QuantityBasisDeclaration.model_validate({
        "recipe_id": snapshot.recipe_id, "baseline_run_id": snapshot.run_id,
        "quantity_input_hash": snapshot.quantity_input_hash, "position": 0, "weight_g": 200,
        "basis": "raw_edible", "declared_by": "synthetic-reviewer", "statement": "Synthetic source review.",
        "evidence": {"reference": "synthetic-source", "method": "source_review", "version": "1", "status": "stated"}})
    preview = run_preview(inputs, quantity_declarations=[declaration])
    draft = editor.draft_raw_part_instructions(snapshot, preview, preview.candidates[0].candidate_id,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."])
    result = service.assemble_adaptation_response(snapshot, preview, draft)
    assert result.adapted_recipe is not None, (draft.reasons, result.reasons)
    item = result.adapted_recipe.ingredients[0]
    assert item.quantity.basis == "raw_edible" and item.baseline_quantity.basis == "unknown"
    assert result.provenance["quantity_declarations"] == [declaration.model_dump(mode="json")]
    assert snapshot.ingredients[0].quantity.basis == "unknown"


@pytest.fixture
def active(setup, monkeypatch):
    snapshot, preview, calls, response = setup
    loader = Mock(return_value=snapshot)
    pointer = Mock(return_value={"run_id": snapshot.run_id})
    monkeypatch.setattr(service, "preview_active_raw_part_candidates", Mock(return_value=preview))
    monkeypatch.setattr(service, "load_active_snapshot", loader)
    monkeypatch.setattr(matching_store, "active_profile", pointer)
    return setup, loader, pointer


def run_active(active, **kwargs):
    snapshot, preview, calls, response = active[0]
    return service.adapt_active_raw_part_recipe(snapshot.recipe_id, snapshot.nutrition_source,
        baseline_run_id=snapshot.run_id, quantity_input_hash=snapshot.quantity_input_hash,
        title="Chicken dish", instructions=["Cook the chicken thigh gently."], target_position=0,
        objective=preview.objective, **kwargs)


def test_active_orchestrator_returns_dictionary_and_chooses_highest_ranked_candidate(active):
    result = run_active(active)
    assert isinstance(result, dict) and result["status"] == "draft_requires_review"
    assert result["adapted_recipe"]["nutrition"]["per_serving"]["energy_kcal"] == 100
    assert result["changes"][0]["edit"]["to_composition"]["candidate_id"] == "breast-b"


def test_explicit_candidate_is_honoured(active):
    result = run_active(active, candidate_id=active[0][1].candidates[0].candidate_id)
    assert result["changes"][0]["edit"]["to_composition"]["candidate_id"] == "breast-a"


def test_active_change_after_editor_discards_recipe(active):
    active[2].return_value = {"run_id": "changed"}
    result = run_active(active)
    assert result["status"] == "blocked" and result["adapted_recipe"] is None and not result["changes"]
    assert result["reasons"] == ["active_profile_changed"]


def test_unknown_candidate_never_runs_editor_or_loader(active):
    result = run_active(active, candidate_id="unknown")
    assert result["status"] == "blocked" and result["adapted_recipe"] is None
    active[1].assert_not_called()
    assert not active[0][2]


@pytest.mark.parametrize("status", ["needs_evidence", "changes_required", "no_supported_candidates", "blocked"])
def test_unsupported_preview_preserves_outcome_without_recipe(active, status):
    active[0][1].status = status
    result = run_active(active)
    assert result["status"] == status and result["adapted_recipe"] is None
    active[1].assert_not_called()
    assert not active[0][2]


def test_editor_failure_has_no_completed_recipe(active):
    active[0][3]["error"] = ValueError("bad response")
    result = run_active(active)
    assert result["status"] == "unavailable" and result["adapted_recipe"] is None


def test_database_failure_propagates(active):
    active[1].side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError): run_active(active)


def test_response_schema_cannot_label_empty_recipe_as_success():
    with pytest.raises(ValidationError):
        RecipeAdaptationResponse(status="draft_requires_review", original_recipe_id="r", baseline_run_id="run",
            quantity_input_hash="q", objective={"metric": "energy_kcal"})


def test_constraints_remain_visible_when_preview_has_no_context(active):
    preview = active[0][1]
    preview.status, preview.context = "blocked", None
    constraints = ComparisonConstraints(forbidden_allergens={"milk"})
    result = run_active(active, constraints=constraints)
    assert result["constraints"]["forbidden_allergens"] == ["milk"]
    assert result["constraint_status"] == "not_evaluated" and result["adapted_recipe"] is None


def test_conditional_changes_remain_visible_without_a_recipe(inputs, model):
    model[1].update({item.ref.candidate_id: "suitable_with_changes" for item in inputs[1]})
    preview = run_preview(inputs)
    result = service.assemble_adaptation_response(inputs[0], preview)
    assert result.status == "changes_required" and result.adapted_recipe is None
    assert result.candidate_diagnostics[0]["culinary"]["required_changes"]
