"""Connected recipe-to-card flow with deterministic calculations and mocked LLMs."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement import breakdown
from test_adaptation_objective_approval import context
from recipe_wrangler.services.adaptation import enhancement_pipeline as service
from recipe_wrangler.services.adaptation.models import ComparisonConstraints
from recipe_wrangler.services.adaptation.models import OccurrenceSourceEvidence
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.services.adaptation.comparison_store import ComparisonLoadError


@pytest.fixture
def flow(inputs, model, setup, catalogue):
    inputs[0].ingredients[0].composition.values_per_100g["energy_kcal"] = 600
    catalogue(inputs[1])
    model[0].clear()
    setup[2].clear()
    return inputs, model, setup


def run(flow, **kwargs):
    inputs, model, setup = flow
    return service.enhance_recipe(inputs[0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        nutri_score_breakdown=breakdown(energy=7), score_context=kwargs.pop("score_context", context(inputs[0])), **kwargs)


def test_recipe_in_returns_complete_adapted_card_in_requested_stage_order(flow):
    inputs, model, setup = flow
    before = deepcopy(inputs[0].model_dump())
    result = run(flow)
    assert result["status"] == "draft_requires_review"
    assert result["stages"] == ["deterministic_screening", "culinary_context_assessment",
        "nutrition_recalculation_and_final_approval", "instruction_editing", "recipe_card_assembly"]
    card = result["adapted_recipe"]
    assert card["ingredients"][0]["name"] == "chicken breast"
    assert card["instructions"] == ["Cook the chicken breast gently."]
    assert card["nutrition"]["per_serving"]["energy_kcal"] == 100
    assert card["nutrition"]["input_score_estimate"]["status"] == "approved_input_objective"
    assert card["nutrition"]["nutri_score"] is None
    assert len(model[0]) == 2 and len(setup[2]) == 1
    assert inputs[0].model_dump() == before
    assert not result["saved"] and not result["recipe_ready"]
    json.dumps(result, allow_nan=False)


def test_selected_source_option_reaches_complete_card_and_llm_patch_allowlist(flow):
    item = flow[0][0].ingredients[0]
    first = item.original_text
    item.original_text += " (or 100 g chicken breast)"
    item.source_evidence = OccurrenceSourceEvidence(recipe_id="recipe-1", baseline_run_id="run-1",
        quantity_input_hash="quantity-1", position=0, weight_g=item.quantity.weight_g,
        source_line=item.original_text, source_hash=digest(item.original_text), selected_source_text=first)
    before = flow[0][0].model_dump()
    result = run(flow)
    assert result["status"] == "draft_requires_review", result["reasons"]
    card = result["adapted_recipe"]
    assert card["ingredients"][0]["original_text"] == "200 g raw boneless skinless chicken breast"
    assert card["ingredients"][0]["quantity"]["weight_g"] == 200
    assert card["serves"] == 2
    assert card["instructions"] == ["Cook the chicken breast gently."]
    assert card["nutrition"]["per_serving"]["energy_kcal"] == 100
    assert card["nutrition"]["input_score_estimate"]["status"] == "approved_input_objective"
    assert len(flow[1][0]) == 2 and len(flow[2][2]) == 1
    allowed = flow[2][2][0][0]["allowed_patches"]
    assert any(p["before"] == " (or 100 g chicken breast)" and p["after"] == "" for p in allowed)
    assert result["saved"] is False and result["recipe_ready"] is False
    assert flow[0][0].model_dump() == before


def test_production_estimate_flows_to_a_review_required_card_without_becoming_verified(flow, monkeypatch):
    from recipe_wrangler.services.adaptation import candidate_selection
    monkeypatch.setattr(candidate_selection, "nutritional_variant_records", lambda *args: [])
    snapshot, targets = flow[0]
    snapshot.input_policy = "production_estimate"
    quantity = snapshot.ingredients[0].quantity
    quantity.basis = "selected_state_edible"
    quantity.evidence.status = "proxy"
    quantity.evidence.method = "saved_production_input_estimate"
    for properties in [snapshot.ingredients[0].observed_properties,
                       snapshot.ingredients[0].composition.properties] + [target.properties for target in targets]:
        for key in list(properties):
            if key.startswith("qualifiers."):
                del properties[key]
    score = context(snapshot)
    score.basis = "selected_state_input_estimate"
    result = run(flow, score_context=score)
    assert result["status"] == "draft_requires_review"
    card = result["adapted_recipe"]
    assert card["nutrition"]["basis"] == "selected_state_input_estimate"
    assert card["nutrition"]["input_score_estimate"]["basis"] == "selected_state_input_estimate"
    assert card["ingredients"][0]["quantity"]["evidence"]["status"] == "proxy"
    assert card["nutrition"]["nutri_score"] is None
    assert any("remain unknown" in text for text in result["uncertainties"])
    assert not result["saved"] and not result["recipe_ready"]


def test_context_can_reject_numerically_best_candidate(flow):
    flow[1][1]["breast-b"] = "unsuitable"
    result = run(flow)
    assert result["status"] == "draft_requires_review"
    assert result["adapted_recipe"]["nutrition"]["per_serving"]["energy_kcal"] == 140
    assert len(flow[2][2]) == 1


def test_known_grade_failures_are_removed_before_any_model_call(flow):
    flow[0][0].ingredients[0].composition.values_per_100g["energy_kcal"] = 200
    result = run(flow, nutrition_policy="strict_grade_v2")
    assert result["status"] == "no_supported_candidates" and result["adapted_recipe"] is None
    assert not flow[1][0] and not flow[2][2]
    assert not result["objective_decisions"] and not result["model_calls"]
    assert all(item["objective_preflight"]["status"] == "rejected" for item in result["candidates"])


def test_preflight_falls_through_to_valid_candidate_without_more_model_calls(flow):
    # Best energy reduction introduces other negative points; the second does not.
    flow[0][1][1].values_per_100g.update(sugar_g=30, saturated_fat_g=15, sodium_mg=1000)
    result = run(flow)
    assert result["status"] == "draft_requires_review"
    assert [item["status"] for item in result["objective_decisions"]] == ["approved_input_objective"]
    rejected = next(item for item in result["candidates"] if item["composition"]["candidate_id"] == "breast-b")
    assert rejected["objective_preflight"]["status"] == "rejected"
    assert rejected["culinary_status"] == "not_evaluated"
    assert result["adapted_recipe"]["nutrition"]["per_serving"]["energy_kcal"] == 140
    assert len(flow[1][0]) == 2 and len(flow[2][2]) == 1


@pytest.mark.parametrize("verdict,status", [("unsuitable", "no_supported_candidates"),
    ("suitable_with_changes", "changes_required"), ("uncertain", "needs_evidence")])
def test_context_changes_and_uncertainty_never_become_executable_operations(flow, verdict, status):
    for name in ("breast-a", "breast-b"): flow[1][1][name] = verdict
    result = run(flow)
    assert result["status"] == status and result["adapted_recipe"] is None
    assert not result["objective_decisions"] and not flow[2][2]


@pytest.mark.parametrize("issue", ["missing", "scope", "inferred", "mass"])
def test_missing_or_invalid_score_context_never_spends_model_tokens(flow, issue):
    score_context = context(flow[0][0])
    if issue == "missing": score_context = None
    elif issue == "scope": score_context.baseline_run_id = "other"
    elif issue == "mass": score_context.total_weight_g = 300
    else: score_context.evidence.status = "inferred"
    result = run(flow, score_context=score_context)
    assert result["status"] in {"blocked", "needs_evidence"}
    assert not flow[1][0] and not flow[2][2]


def test_missing_allergen_evidence_never_calls_llm(flow):
    result = run(flow, constraints=ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None
    assert not flow[1][0]


def test_known_missing_score_inputs_are_rejected_before_spending_model_tokens(flow):
    for target in flow[0][1]: target.values_per_100g["fibre_g"] = None
    result = run(flow)
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None
    assert not flow[1][0] and not flow[2][2]


def test_editor_cannot_add_unvalidated_recipe_changes(flow):
    flow[2][3]["mutate"] = lambda plan: plan["patches"].append({"kind": "instruction", "reference_id": "step:0",
        "start": 0, "end": 0, "before": "", "after": "Add oil. "})
    result = run(flow)
    assert result["status"] != "draft_requires_review" and result["adapted_recipe"] is None


@pytest.fixture
def active(flow, monkeypatch):
    snapshot = flow[0][0]
    recipe = {"recipe_id": snapshot.recipe_id, "title": "Chicken dish", "instructions": ["Cook the chicken thigh gently."],
        "serves": 2, "original_ingredients": [snapshot.ingredients[0].original_text], "status": "active"}
    monkeypatch.setattr(service, "load_active_snapshot", lambda *args, **kwargs: snapshot)
    fetch = Mock(side_effect=lambda *args: deepcopy(recipe))
    monkeypatch.setattr(service, "fetch_recipe_info_by_id", fetch)
    pointer = Mock(side_effect=lambda *args, **kwargs: {"run_id": snapshot.run_id,
        "profile": {"nutri_score_breakdown": breakdown(energy=7)}})
    monkeypatch.setattr(service.matching_store, "active_profile", pointer)
    return recipe, fetch, pointer


def test_active_adapter_loads_authoritative_text_and_rechecks_it_around_models(flow, active):
    result = service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
    assert result["status"] == "draft_requires_review" and result["recipe_revision"]
    assert active[1].call_count >= 4


@pytest.mark.parametrize("field", ["title", "instructions", "original_ingredients", "serves"])
def test_recipe_changes_during_inference_discard_result(flow, active, field):
    recipe, fetch, pointer = active
    original = service.preview_raw_part_candidates
    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        recipe[field] = "changed"
        return result
    from unittest.mock import patch
    with patch.object(service, "preview_raw_part_candidates", side_effect=preview):
        result = service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
    assert result["status"] == "blocked" and result["reasons"] == ["recipe_text_changed"]
    assert result["adapted_recipe"] is None and not flow[2][2]


def test_profile_changes_after_editing_discard_card(flow, active, monkeypatch):
    original = service.draft_raw_part_instructions
    def draft(*args, **kwargs):
        result = original(*args, **kwargs)
        active[2].side_effect = lambda *args, **kwargs: {"run_id": "changed"}
        return result
    monkeypatch.setattr(service, "draft_raw_part_instructions", draft)
    result = service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
    assert result["status"] == "blocked" and result["reasons"] == ["active_profile_changed"]
    assert result["adapted_recipe"] is None


def test_profile_and_graph_ingredients_must_match_before_inference(flow, active):
    active[0]["original_ingredients"] = ["different ingredient"]
    result = service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
    assert result["status"] == "blocked" and result["adapted_recipe"] is None
    assert not flow[1][0]


@pytest.mark.parametrize("source,saved", [("½ stock cube", "1/2 stock cube"),
    ("1½ pint chicken stock", "1 1/2 pint chicken stock"),
    ("1 ½ teaspoon curry powder", "1 1/2 teaspoon curry powder")])
def test_source_binding_accepts_parser_fraction_representation_only(flow, active, source, saved):
    snapshot = flow[0][0]
    snapshot.ingredients[0].original_text = saved
    active[0]["original_ingredients"] = [source]
    before = snapshot.model_dump(), deepcopy(active[0])
    service._verify_source_binding(snapshot, active[0], {})
    assert (snapshot.model_dump(), active[0]) == before
    assert not flow[1][0] and not flow[2][2]


@pytest.mark.parametrize("changed", ["1 stock cube", "1/4 stock cube", "½ chicken stock cube",
    "½ stock cube or water", "½ stock cube (optional)", "11/2 stock cube"])
def test_fraction_binding_does_not_hide_quantity_identity_or_scope_changes(flow, active, changed):
    snapshot = flow[0][0]
    snapshot.ingredients[0].original_text = "1/2 stock cube"
    active[0]["original_ingredients"] = [changed]
    with pytest.raises(ComparisonLoadError, match="recipe_ingredient_text_does_not_match_profile"):
        service._verify_source_binding(snapshot, active[0], {})


def test_unique_reordered_source_lines_bind_without_changing_profile_or_source(flow, active):
    snapshot = flow[0][0]
    second = snapshot.ingredients[0].model_copy(deep=True)
    second.position, second.original_text = 1, '100g rice'
    snapshot.ingredients.append(second)
    active[0]['original_ingredients'] = ['100g rice', snapshot.ingredients[0].original_text]
    before = snapshot.model_dump(), deepcopy(active[0])
    bindings = service._verify_source_binding(snapshot, active[0], {})
    assert [(b['profile_position'], b['source_position']) for b in bindings] == [(0, 1), (1, 0)]
    assert (snapshot.model_dump(), active[0]) == before
    assert not flow[1][0] and not flow[2][2]


def test_reordered_duplicate_occurrences_require_explicit_review(flow, active):
    snapshot = flow[0][0]
    first = snapshot.ingredients[0]
    for position, text in enumerate(['100g rice', first.original_text], start=1):
        other = first.model_copy(deep=True)
        other.position, other.original_text = position, text
        snapshot.ingredients.append(other)
    active[0]['original_ingredients'] = [first.original_text, first.original_text, '100g rice']
    with pytest.raises(ComparisonLoadError, match='reordered_repeated_lines'):
        service._verify_source_binding(snapshot, active[0], {})


@pytest.mark.parametrize("issue", ["missing_original", "split_fragments"])
def test_missing_source_binding_is_an_evidence_gap_rather_than_a_confirmed_edit(flow, active, issue):
    if issue == "missing_original":
        flow[0][0].ingredients[0].original_text = ""
        active[0]["original_ingredients"][0] = "100g unrelated food"
    else: active[0]["original_ingredients"].append("additional source fragment")
    result = service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None
    assert not flow[1][0]


def test_duplicate_source_lines_are_explained_without_dropping_occurrences_or_calling_models(flow, active):
    active[0]['original_ingredients'] *= 2
    before = deepcopy(active[0])
    result = service.enhance_active_recipe('recipe-1', 'irish', score_context=context(flow[0][0]))
    assert result['status'] == 'needs_evidence' and result['adapted_recipe'] is None
    assert result['reasons'] == ['source_recipe_repeated_lines_require_verification']
    assert 'repeats lines' in result['explanation']
    assert active[0] == before
    assert not flow[1][0] and not flow[2][2]


def test_database_failure_is_not_disguised_as_no_improvement(flow, active):
    active[2].side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError):
        service.enhance_active_recipe("recipe-1", "irish", score_context=context(flow[0][0]))
