"""Automatic target-to-shortlist flow, with real comparisons and no paid calls."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from test_adaptation_preview import inputs
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement import breakdown
from recipe_wrangler.repositories import matching_store
from recipe_wrangler.services.adaptation import enhancement_screening as service
from recipe_wrangler.services.adaptation import contextual_assessment, instruction_editor
from recipe_wrangler.services.adaptation.models import ComparisonConstraints, QuantityBasisDeclaration
from recipe_wrangler.services.adaptation.comparison_store import ComparisonLoadError


@pytest.fixture(autouse=True)
def forbid_model_calls(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Candidate screening must not call an LLM")
    monkeypatch.setattr(contextual_assessment, "_request", fail)
    monkeypatch.setattr(instruction_editor, "_request", fail)


def screen(snapshot, **kwargs):
    return service.screen_enhancement(snapshot, mode="nutrition",
        nutri_score_breakdown=breakdown(energy=5), **kwargs)


def append_occurrence(snapshot, position, *, weight=200):
    item = snapshot.ingredients[0].model_copy(deep=True)
    item.position, item.quantity.weight_g = position, weight
    snapshot.ingredients.append(item)
    return item


def test_automatic_nutrition_target_reaches_real_shortlist_without_caller_positions(inputs, catalogue):
    query, _ = catalogue(inputs[1])
    before = deepcopy(inputs[0].model_dump())
    result = screen(inputs[0])
    assert result["status"] == "candidates_screened"
    assert result["selected_target_position"] == 0
    assert [ref["candidate_id"] for ref in result["candidate_refs"]] == ["breast-b", "breast-a"]
    comparison = result["attempts"][0]["shortlist"]["screening"][0]["comparison"]
    assert comparison["status"] == "supported_input_comparison"
    assert result["objective_approval"] == "pending" and result["adapted_recipe"] is None
    assert not result["recipe_ready"] and not result["saved"]
    assert inputs[0].model_dump() == before
    query.assert_called_once()
    json.dumps(result, allow_nan=False)


def test_ungrounded_top_contributor_falls_through_without_catalogue_lookup(inputs, catalogue):
    other = append_occurrence(inputs[0], 1, weight=500)
    other.composition.properties.pop("food_part")
    query, _ = catalogue(inputs[1])
    result = screen(inputs[0])
    assert [item["position"] for item in result["attempts"]] == [1, 0]
    assert result["attempts"][0]["status"] == "needs_evidence"
    assert result["selected_target_position"] == 0
    query.assert_called_once()


def test_empty_first_shortlist_falls_through_to_second_occurrence(inputs, catalogue):
    # More grams makes the low-energy occurrence the largest contributor, but
    # neither available breast record improves its nutrient contribution.
    inputs[0].ingredients[0].quantity.weight_g = 1000
    inputs[0].ingredients[0].composition.values_per_100g["energy_kcal"] = 80
    other = append_occurrence(inputs[0], 1)
    other.composition.values_per_100g["energy_kcal"] = 200
    query, _ = catalogue(inputs[1])
    result = screen(inputs[0])
    assert result["attempts"][0]["status"] == "no_supported_candidates"
    assert result["selected_target_position"] == 1 and query.call_count == 2


def test_quick_candidates_across_targets_precede_expensive_graph_fallback(inputs, catalogue, monkeypatch):
    from recipe_wrangler.services.adaptation import candidate_selection
    snapshot = inputs[0]
    snapshot.input_policy = 'production_estimate'
    snapshot.ingredients[0].quantity.weight_g = 1000
    snapshot.ingredients[0].composition.values_per_100g['energy_kcal'] = 80
    other = append_occurrence(snapshot, 1)
    other.composition.values_per_100g['energy_kcal'] = 200
    catalogue(inputs[1])
    monkeypatch.setattr(candidate_selection, 'nutritional_variant_records', lambda *a: [])
    graph = Mock(side_effect=AssertionError('Try the next quick target before graph search'))
    monkeypatch.setattr(candidate_selection, 'production_candidate_records', graph)
    result = screen(snapshot, allow_general_substitutions=True)
    assert result['status'] == 'candidates_screened'
    assert result['selected_target_position'] == 1
    graph.assert_not_called()


def test_search_reaches_fourth_after_three_ungrounded_occurrences(inputs, catalogue):
    for position in range(1, 4):
        other = append_occurrence(inputs[0], position, weight=1000-position)
        other.composition.properties.pop("food_part")
    query, _ = catalogue(inputs[1])
    result = screen(inputs[0])
    assert result["status"] == "candidates_screened"
    assert [item["position"] for item in result["attempts"]] == [1, 2, 3, 0]
    assert result["selected_target_position"] == 0
    query.assert_called_once()


def test_candidate_scan_truncation_reports_search_budget(inputs, monkeypatch):
    from recipe_wrangler.services.adaptation.models import RawPartShortlist
    def truncated(snapshot, **kwargs):
        return RawPartShortlist(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
            quantity_input_hash=snapshot.quantity_input_hash, target_position=kwargs['target_position'],
            status='no_supported_candidates', reasons=['production_candidate_scan_limit_reached'])
    monkeypatch.setattr(service, 'shortlist_raw_part_candidates', truncated)
    result = screen(inputs[0], goal_nutrients=['energy'])
    assert result['reasons'] == ['search_budget_reached']


@pytest.mark.parametrize("issue", ["observed_conflict", "missing_source_fact", "missing_candidate_fact", "allergen", "diet"])
def test_discovery_does_not_override_evidence_or_constraints(inputs, catalogue, issue):
    constraints = None
    if issue == "observed_conflict": inputs[0].ingredients[0].observed_properties["base_identity"].value = "beef"
    elif issue == "missing_source_fact": inputs[0].ingredients[0].composition.properties.pop("food_part")
    elif issue == "missing_candidate_fact":
        for item in inputs[1]: item.properties.pop("bone_status")
    elif issue == "allergen": constraints = ComparisonConstraints(forbidden_allergens={"milk"})
    else: constraints = ComparisonConstraints(required_consumer_groups={"vegan"})
    catalogue(inputs[1])
    result = screen(inputs[0], constraints=constraints)
    assert result["status"] in {"needs_evidence", "no_supported_candidates"}
    assert not result["candidate_refs"] and result["selected_target_position"] is None
    if constraints: assert result["constraints"] == constraints.model_dump(mode="json")


def test_incomplete_unchanged_occurrence_prevents_complete_objective_gain(inputs, catalogue):
    other = append_occurrence(inputs[0], 1, weight=5)
    other.composition.values_per_100g["energy_kcal"] = None
    catalogue(inputs[1])
    result = screen(inputs[0])
    assert result["status"] == "needs_evidence" and not result["candidate_refs"]
    assert result["target_plan"]["complete_total"] is None
    assert result["target_plan"]["unknown_contributions"][0]["position"] == 1


def test_no_target_never_retrieves_candidates(inputs, catalogue):
    query, _ = catalogue(inputs[1])
    points = breakdown(energy=0)
    points["negative_points"]["items"]["energy"]["value_per_100g"] = 0
    result = service.screen_enhancement(inputs[0], mode="nutrition", nutri_score_breakdown=points)
    assert result["status"] == "no_target" and not result["attempts"]
    query.assert_not_called()


def test_explicit_quantity_declaration_is_forwarded_without_mutating_baseline(inputs, catalogue):
    snapshot = inputs[0]
    snapshot.ingredients[0].quantity.basis = "unknown"
    declaration = QuantityBasisDeclaration.model_validate({
        "recipe_id": snapshot.recipe_id, "baseline_run_id": snapshot.run_id,
        "quantity_input_hash": snapshot.quantity_input_hash, "position": 0, "weight_g": 200,
        "basis": "raw_edible", "declared_by": "reviewer", "statement": "Raw edible weight confirmed",
        "evidence": {"reference": "review-1", "method": "source_review", "version": "1", "status": "stated"}})
    catalogue(inputs[1])
    result = screen(snapshot, quantity_declarations=[declaration])
    assert result["status"] == "candidates_screened"
    comparison = result["attempts"][0]["shortlist"]["screening"][0]["comparison"]
    assert comparison["quantity_declarations"][0]["declared_by"] == "reviewer"
    assert snapshot.ingredients[0].quantity.basis == "unknown"


@pytest.mark.parametrize("mode", ["sustainability", "cost"])
def test_proxy_goals_never_get_a_nutrient_objective_as_a_substitute(inputs, catalogue, monkeypatch, mode):
    from recipe_wrangler.pricing import cost_calculator
    from recipe_wrangler.repositories import sustainability_references
    from adaptation_reference_fixtures import emissions_reference
    name = inputs[0].ingredients[0].name
    monkeypatch.setattr(sustainability_references, "lookup_references", Mock(side_effect=[
        ({name: emissions_reference(name, 6)}, "available"), ({}, "available")]))
    monkeypatch.setattr(cost_calculator, "load_cost_catalogue", lambda: Mock(resolve=Mock(return_value={
        "match_status": "matched", "economic_reference_price_eur_kg": 10})))
    query, _ = catalogue(inputs[1])
    result = service.screen_enhancement(inputs[0], mode=mode, cost_country="IE")
    assert result["status"] == "needs_evidence"
    assert result["numerical_objective"] is None and not result["candidate_refs"]
    assert result["attempts"][0]["reasons"] == ["replacement_specific_objective_comparison_required"]
    query.assert_not_called()


@pytest.mark.parametrize("changed_after", [0, 1, None])
def test_active_wrapper_checks_run_before_and_after_candidate_reads(inputs, catalogue, monkeypatch, changed_after):
    snapshot = inputs[0]
    query, _ = catalogue(inputs[1])
    monkeypatch.setattr(service, "load_active_snapshot", lambda *args: snapshot)
    row = {"run_id": snapshot.run_id, "profile": {"nutri_score_breakdown": breakdown(energy=5)}}
    rows = [deepcopy(row), deepcopy(row)]
    if changed_after is not None: rows[changed_after]["run_id"] = "changed"
    monkeypatch.setattr(matching_store, "active_profile", Mock(side_effect=rows))
    if changed_after is None:
        result = service.screen_active_enhancement(snapshot.recipe_id, snapshot.nutrition_source, mode="nutrition")
        assert result["status"] == "candidates_screened"
    else:
        with pytest.raises(ComparisonLoadError, match="active_profile_changed"):
            service.screen_active_enhancement(snapshot.recipe_id, snapshot.nutrition_source, mode="nutrition")
        if changed_after == 0: query.assert_not_called()


def test_database_outage_is_not_reported_as_no_candidates(inputs, catalogue):
    query, _ = catalogue(inputs[1])
    query.side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError): screen(inputs[0])
