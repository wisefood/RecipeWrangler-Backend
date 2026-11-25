"""Production goal targeting without model calls or fabricated coverage."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_adaptation_preview import inputs
from adaptation_reference_fixtures import emissions_reference
from recipe_wrangler.services.adaptation import enhancement as service
from recipe_wrangler.services.adaptation.comparison_store import ComparisonLoadError
from recipe_wrangler.services.adaptation.nutrition_benefit import DEFAULT_POLICY, STRICT_POLICY


def breakdown(**points):
    return {"negative_points": {"items": {key: {"points": value, "value_per_100g": 10}
            for key, value in points.items()}}}


def test_nutrition_uses_production_negative_points_and_absolute_occurrence_contributions(inputs):
    snapshot = inputs[0]
    second = snapshot.ingredients[0].model_copy(deep=True)
    second.position, second.name, second.quantity.weight_g = 1, "smaller chicken portion", 5
    snapshot.ingredients.append(second)
    original = deepcopy(snapshot.model_dump())
    result = service.plan_enhancement(snapshot, mode="nutrition", nutri_score_breakdown=breakdown(energy=5, sugar=2))
    assert result["objective"]["metric"] == "energy_kcal"
    assert [item["position"] for item in result["ranked_targets"]] == [0, 1]
    assert result["complete_total"] == 410
    assert result["execution_status"] == "not_executed" and result["adapted_recipe"] is None
    assert snapshot.model_dump() == original


def test_equal_penalties_prioritize_saturated_fat_independent_of_field_order(inputs):
    for points in (breakdown(sodium=2, saturated_fats=2), breakdown(saturated_fats=2, sodium=2)):
        result = service.plan_enhancement(inputs[0], mode='nutrition', nutri_score_breakdown=points)
        assert result['objective']['metric'] == 'saturated_fat_g'


def test_member_goals_only_bias_nutrients_above_production_threshold(inputs):
    result = service.plan_enhancement(inputs[0], mode="nutrition",
        nutri_score_breakdown=breakdown(energy=6, sugar=3), goal_nutrients=["reduce_sugar"], nutrition_policy=STRICT_POLICY)
    assert result["objective"]["metric"] == "sugar_g"
    result = service.plan_enhancement(inputs[0], mode="nutrition",
        nutri_score_breakdown=breakdown(energy=6, sugar=2), goal_nutrients=["reduce_sugar"], nutrition_policy=STRICT_POLICY)
    assert result["objective"]["metric"] == "energy_kcal"


@pytest.mark.parametrize("goals",[None,[],["unsupported"],["protein"]])
def test_within_grade_pilot_requires_an_explicit_supported_goal(inputs,goals):
    result=service.plan_enhancement(inputs[0],mode="nutrition",nutri_score_breakdown=breakdown(energy=0),
        goal_nutrients=goals,nutrition_policy="within_grade_pilot_v1")
    assert result["status"]=="needs_evidence" and result["objective"] is None
    assert result["reasons"]==["within_grade_pilot_requires_explicit_nutrient_goal"]


def test_within_grade_pilot_uses_requested_zero_point_nutrient_without_switching_goals(inputs):
    result=service.plan_enhancement(inputs[0],mode="nutrition",
        nutri_score_breakdown=breakdown(energy=0,sodium=5),goal_nutrients=["energy"],
        nutrition_policy="within_grade_pilot_v1")
    assert result["objective"]["metric"]=="energy_kcal"
    assert result["objective"]["target_negative_points"]==0
    assert result["objective"]["benefit_policy"]=="within_grade_pilot_v1"
    result=service.plan_enhancement(inputs[0],mode="nutrition",
        nutri_score_breakdown=breakdown(sodium=5),goal_nutrients=["energy"],
        nutrition_policy="within_grade_pilot_v1")
    assert result["status"]=="no_target" and result["objective"] is None


@pytest.mark.parametrize("points,status", [(None, "needs_evidence"), (breakdown(energy=2), "no_target")])
def test_no_target_does_not_invent_an_objective(inputs, points, status):
    result = service.plan_enhancement(inputs[0], mode="nutrition", nutri_score_breakdown=points, nutrition_policy=STRICT_POLICY)
    assert result["status"] == status and result["objective"] is None


def test_default_discovers_requested_zero_point_nutrient_instead_of_switching_goals(inputs):
    result=service.plan_enhancement(inputs[0],mode="nutrition",
        nutri_score_breakdown=breakdown(energy=0,sodium=5),goal_nutrients=["energy"])
    assert result["objective"]["metric"]=="energy_kcal"
    assert result["objective"]["benefit_policy"]==DEFAULT_POLICY


def test_default_button_discovers_positive_zero_point_nutrient_without_a_goal(inputs):
    points=breakdown(energy=0,sugar=0)
    points["negative_points"]["items"]["energy"]["value_per_100g"]=0
    result=service.plan_enhancement(inputs[0],mode="nutrition",nutri_score_breakdown=points)
    assert result["objective"]["metric"]=="sugar_g"
    assert result["objective"]["target_negative_points"]==0


@pytest.mark.parametrize("goals", [["unsupported"],["protein"]])
def test_default_does_not_silently_replace_an_unsupported_requested_goal(inputs,goals):
    result=service.plan_enhancement(inputs[0],mode="nutrition",
        nutri_score_breakdown=breakdown(energy=0),goal_nutrients=goals)
    assert result["status"]=="needs_evidence" and result["objective"] is None


def test_default_does_not_invent_a_zero_or_missing_requested_nutrient(inputs):
    points=breakdown(energy=0,sodium=5)
    points["negative_points"]["items"]["energy"]["value_per_100g"]=0
    result=service.plan_enhancement(inputs[0],mode="nutrition",nutri_score_breakdown=points,goal_nutrients=["energy"])
    assert result["status"]=="no_target" and result["objective"] is None


@pytest.mark.parametrize("mutation,reason", [
    ("quantity", "raw_edible_quantity_evidence_required"),
    ("nutrient", "target_nutrient_unknown"),
    ("cooked", "raw_composition_evidence_required"),
    ("proxy_basis", "supported_composition_basis_required"),
])
def test_unknown_or_incompatible_contributions_never_become_complete_zero(inputs, mutation, reason):
    ingredient = inputs[0].ingredients[0]
    if mutation == "quantity": ingredient.quantity.basis = "unknown"
    elif mutation == "nutrient": ingredient.composition.values_per_100g["energy_kcal"] = None
    elif mutation == "cooked": ingredient.composition.properties["preparation_state"].value = "cooked"
    else: ingredient.composition.basis.evidence.status = "proxy"
    result = service.plan_enhancement(inputs[0], mode="nutrition", nutri_score_breakdown=breakdown(energy=5))
    assert result["status"] == "needs_evidence"
    assert result["supported_partial_total"] == 0 and result["complete_total"] is None
    assert result["unknown_contributions"][0]["reason"] == reason


def test_cost_ranking_keeps_catalogue_matches_as_proxies_and_requires_country(inputs, monkeypatch):
    from recipe_wrangler.pricing import cost_calculator
    catalogue = Mock()
    catalogue.resolve.return_value = {"match_status": "matched", "economic_reference_price_eur_kg": 10}
    loader = Mock(return_value=catalogue)
    monkeypatch.setattr(cost_calculator, "load_cost_catalogue", loader)
    assert service.plan_enhancement(inputs[0], mode="cost")["status"] == "needs_evidence"
    loader.assert_not_called()
    result = service.plan_enhancement(inputs[0], mode="cost", cost_country="ie")
    assert result["complete_total"] == 2
    assert result["ranked_targets"][0]["provenance"]["status"] == "proxy"
    assert result["objective"]["minimum_relative_reduction"] == .1
    catalogue.resolve.assert_called_once_with("chicken thigh", "IE")


@pytest.mark.parametrize("factor", [None, float("nan"), -1, True])
def test_missing_emissions_never_become_zero(inputs, monkeypatch, factor):
    from recipe_wrangler.repositories import sustainability_references
    monkeypatch.setattr(sustainability_references, "lookup_references", lambda names:
        ({name: emissions_reference(name, factor) for name in names}, "available"))
    result = service.plan_enhancement(inputs[0], mode="sustainability")
    assert result["complete_total"] is None and result["status"] == "needs_evidence"


def test_sustainability_uses_absolute_mass_and_production_reduction_threshold(inputs, monkeypatch):
    from recipe_wrangler.repositories import sustainability_references
    monkeypatch.setattr(sustainability_references, "lookup_references", lambda names:
        ({name: emissions_reference(name, 6) for name in names}, "available"))
    result = service.plan_enhancement(inputs[0], mode="sustainability")
    assert result["complete_total"] == pytest.approx(1.2)
    assert result["objective"]["minimum_relative_reduction"] == .1
    assert result["ranked_targets"][0]["provenance"]["status"] == "proxy"
    assert "replacement_specific_factor_provenance" in result["required_execution_checks"]


@pytest.mark.parametrize("changed_after", [0, 1, None])
def test_active_wrapper_pins_goal_breakdown_to_snapshot_run(inputs, monkeypatch, changed_after):
    from recipe_wrangler.repositories import matching_store
    from recipe_wrangler.services.adaptation import comparison_store
    snapshot = inputs[0]
    monkeypatch.setattr(comparison_store, "load_active_snapshot", lambda *args: snapshot)
    row = {"run_id": snapshot.run_id, "profile": {"nutri_score_breakdown": breakdown(energy=5)}}
    rows = [deepcopy(row), deepcopy(row)]
    if changed_after is not None: rows[changed_after]["run_id"] = "changed"
    monkeypatch.setattr(matching_store, "active_profile", Mock(side_effect=rows))
    if changed_after is not None:
        with pytest.raises(ComparisonLoadError, match="active_profile_changed"):
            service.plan_active_enhancement(snapshot.recipe_id, snapshot.nutrition_source, mode="nutrition")
    else:
        result = service.plan_active_enhancement(snapshot.recipe_id, snapshot.nutrition_source, mode="nutrition")
        assert result["baseline_run_id"] == snapshot.run_id
        assert result["objective"]["metric"] == "energy_kcal"


def test_unknown_mode_rejected(inputs):
    with pytest.raises(ValueError, match="Unsupported enhancement mode"):
        service.plan_enhancement(inputs[0], mode="typo")
