"""Reuse production score/reference policies without live services or paid calls."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow, active
from test_adaptation_objective_approval import context
from adaptation_reference_fixtures import emissions_reference
from recipe_wrangler.services.adaptation import enhancement_pipeline as pipeline
from recipe_wrangler.services.adaptation.objective_approval import production_score_context, approve_nutrition_candidate
from recipe_wrangler.services.adaptation.impact_objectives import objective_delta
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.services.adaptation.nutrition_benefit import DEFAULT_POLICY
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values


def profile(snapshot):
    values = snapshot.ingredients[0].composition.values_per_100g
    mass = snapshot.ingredients[0].quantity.weight_g
    totals = {key: value * mass / 100 for key, value in values.items()}
    score = compute_nutri_score_breakdown_from_values({"energy": values["energy_kcal"] * 4.184,
        "sugar": values["sugar_g"], "saturated_fats": values["saturated_fat_g"], "sodium": values["sodium_mg"],
        "fibers": values["fibre_g"], "proteins": values["protein_g"], "fruit_percentage": 0.}, "solid")
    score["inputs"] = {"total_weight_g": mass}
    return {"nutri_score_breakdown": score, "clean_totals": totals, "quantity_input_hash": snapshot.quantity_input_hash}


def test_automatic_context_retains_production_estimates_as_proxy(inputs):
    saved = profile(inputs[0])
    result = production_score_context(inputs[0], saved)
    assert result is not None and result.evidence.status == "proxy"
    assert result.fruit_veg_legume_percent == 0 and result.food_type == "solid"


@pytest.mark.parametrize("issue", ["missing", "mass", "nutrient", "nan", "food_type", "fvl", "hash"])
def test_automatic_context_never_restores_missing_or_inconsistent_inputs(inputs, issue):
    saved = profile(inputs[0])
    if issue == "missing": saved["nutri_score_breakdown"] = None
    elif issue == "mass": saved["nutri_score_breakdown"]["inputs"]["total_weight_g"] = 100
    elif issue == "nutrient": saved["clean_totals"]["sugar_g"] = None
    elif issue == "nan": saved["clean_totals"]["protein_g"] = float("nan")
    elif issue == "food_type": saved["nutri_score_breakdown"].pop("food_type")
    elif issue == "fvl": saved["nutri_score_breakdown"]["positive_points"]["items"]["fruit_percentage"]["value_per_100g"] = 101
    else: saved["quantity_input_hash"] = "other"
    assert production_score_context(inputs[0], saved) is None


def test_active_button_request_automatically_uses_compatible_production_context(flow, active):
    snapshot = flow[0][0]
    active[2].side_effect = lambda *args, **kwargs: {"run_id": snapshot.run_id, "profile": profile(snapshot)}
    result = pipeline.enhance_active_recipe("recipe-1", "irish")
    assert result["status"] == "draft_requires_review"
    assert result["final_objective_approval"]["score_context"]["evidence"]["status"] == "proxy"
    assert any("proxy" in item for item in result["uncertainties"])


@pytest.fixture
def references(monkeypatch):
    from recipe_wrangler.pricing import cost_calculator
    from recipe_wrangler.repositories import sustainability_references
    matches = {}
    def resolve(name, country):
        default = {"match_status": "matched", "price_scope": "detailed_product", "match_method": "exact_detail:name",
            "matched_product_id": name.lower(), "food_category": "meat", "cost_reference_version": "fixture-v1",
            "economic_reference_price_eur_kg": 10 if "thigh" in name.lower() else 5}
        return matches.get(name, default)
    catalogue = Mock(resolve=Mock(side_effect=resolve))
    monkeypatch.setattr(cost_calculator, "load_cost_catalogue", lambda: catalogue)
    monkeypatch.setattr(sustainability_references, "lookup_references", lambda names:
        ({name: emissions_reference(name, 10 if "thigh" in name.lower() else 5) for name in names}, "available"))
    return matches


@pytest.mark.parametrize("mode,metric", [("cost", "cost_eur"), ("sustainability", "co2e_kg")])
@pytest.mark.parametrize("policy",[DEFAULT_POLICY,"strict_grade_v2","within_grade_pilot_v1"])
def test_impact_modes_execute_real_objective_then_context_and_nonregressing_score_guard(flow, references, mode, metric,policy):
    snapshot = flow[0][0]
    snapshot.ingredients[0].composition.values_per_100g["energy_kcal"] = 200
    result = pipeline.enhance_recipe(snapshot, title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode=mode, score_context=context(snapshot), cost_country="IE",nutrition_policy=policy)
    assert result["status"] == "draft_requires_review"
    estimate = result["adapted_recipe"]["impact_estimate"]
    assert estimate["metric"] == metric and estimate["delta_per_serving"] == -.5
    assert estimate["before_total"] == 2 and estimate["after_total"] == 1
    assert estimate["evidence_status"] == "proxy"
    # Impact modes retain their existing nonworsening-grade rule under every policy.
    approval = result["final_objective_approval"]
    assert approval["before"]["nutri_score"] == approval["after"]["nutri_score"]
    assert len(flow[1][0]) == 2 and len(flow[2][2]) == 1


@pytest.mark.parametrize("issue", ["broad", "version", "version_mismatch", "category", "same_product", "trivial_saving"])
def test_cost_rejects_unsupported_proxies_category_or_insufficient_savings(flow, references, issue):
    target_name = flow[0][1][0].provenance["candidate"]["document"]
    target_name_b = flow[0][1][1].provenance["candidate"]["document"]
    match = {"match_status": "matched", "price_scope": "detailed_product", "match_method": "exact_detail:name",
        "matched_product_id": "breast", "food_category": "meat", "cost_reference_version": "fixture-v1",
        "economic_reference_price_eur_kg": 5}
    if issue == "broad": match["price_scope"] = "foodon_group"
    elif issue == "version": match.pop("cost_reference_version")
    elif issue == "version_mismatch": match["cost_reference_version"] = "other-version"
    elif issue == "category": match["food_category"] = "offal"
    elif issue == "same_product": match["matched_product_id"] = "chicken thigh"
    else: match["economic_reference_price_eur_kg"] = 9.5
    references[target_name] = references[target_name_b] = match
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="cost", score_context=context(flow[0][0]), cost_country="IE")
    assert result["adapted_recipe"] is None and not flow[1][0]


def test_cost_accepts_base_product_alias_matches_as_estimates(flow, references):
    for name in (flow[0][1][0].provenance["candidate"]["document"], flow[0][1][1].provenance["candidate"]["document"]):
        references[name] = {"match_status": "matched", "price_scope": "base_product", "match_method": "reviewed_alias:ingredient_name",
            "matched_product_id": "base__" + name, "food_category": "meat", "cost_reference_version": "fixture-v1",
            "economic_reference_price_eur_kg": 5}
    references["chicken thigh"] = {**references[name], "matched_product_id": "base__thigh", "economic_reference_price_eur_kg": 10}
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="cost", score_context=context(flow[0][0]), cost_country="IE")
    assert result["adapted_recipe"]["impact_estimate"]["evidence_status"] == "proxy"


def test_cost_ignores_nutrition_regression_while_guards_are_off(flow, references):
    for item in flow[0][1]: item.values_per_100g.update(sugar_g=60, sodium_mg=2000, saturated_fat_g=30)
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="cost", score_context=context(flow[0][0]), cost_country="IE")
    assert result["adapted_recipe"]["impact_estimate"]["metric"] == "cost_eur"


@pytest.mark.parametrize("mode", ["cost", "sustainability"])
def test_impact_mode_does_not_allow_nutrition_regression(flow, references, monkeypatch, mode):
    from recipe_wrangler.services.adaptation import objective_approval
    monkeypatch.setattr(objective_approval, "COST_NUTRITION_GUARDS", True)
    for item in flow[0][1]: item.values_per_100g.update(sugar_g=60, sodium_mg=2000, saturated_fat_g=30)
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode=mode, score_context=context(flow[0][0]), cost_country="IE")
    assert result["adapted_recipe"] is None and not flow[2][2]
    assert not result["model_calls"]
    assert all("nutrition_grade_regression" in item["objective_preflight"]["reasons"] for item in result["candidates"])


@pytest.mark.parametrize("mode", ["nutrition", "cost", "sustainability"])
@pytest.mark.parametrize("metric,before,after", [
    ("sodium_mg", 2000., 4000.), ("sugar_g", 60., 70.),
    ("saturated_fat_g", 30., 40.), ("protein_g", 20., 10.), ("fibre_g", 5., 1.),
])
def test_unchanged_or_better_grade_cannot_hide_nutrient_regression(flow, references, monkeypatch, mode, metric, before, after):
    from recipe_wrangler.services.adaptation import objective_approval
    monkeypatch.setattr(objective_approval, "COST_NUTRITION_GUARDS", True)
    source = flow[0][0].ingredients[0].composition
    source.values_per_100g.update(energy_kcal=600., sugar_g=60., saturated_fat_g=30., sodium_mg=2000., fibre_g=5.)
    source.values_per_100g[metric] = before
    for target in flow[0][1]:
        target.values_per_100g.update(sugar_g=60., saturated_fat_g=30., sodium_mg=2000., fibre_g=5.)
        target.values_per_100g[metric] = after
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode=mode, nutri_score_breakdown=profile(flow[0][0])["nutri_score_breakdown"], goal_nutrients=["energy"],
        score_context=context(flow[0][0]), cost_country="IE")
    assert result["adapted_recipe"] is None and not flow[2][2]
    assert not result["objective_decisions"] and not result["model_calls"]
    assert result["candidates"]
    assert all("nutrient_regression:" + metric in item["objective_preflight"]["reasons"] for item in result["candidates"])


@pytest.mark.parametrize("issue", ["missing", "boundary", "basis", "market", "version", "hash"])
def test_sustainability_requires_complete_comparable_source_records(flow, references, monkeypatch, issue):
    from recipe_wrangler.repositories import sustainability_references
    def lookup(names):
        rows = {name: emissions_reference(name, 10 if "thigh" in name.lower() else 5) for name in names}
        for name, row in list(rows.items()):
            if "thigh" in name.lower():
                continue
            if issue == "missing": rows.pop(name)
            elif issue == "hash": row["source_sha256"] = "different-source"
            else: row[issue] = "incompatible"
        return rows, "available"
    monkeypatch.setattr(sustainability_references, "lookup_references", lookup)
    flow[0][0].ingredients[0].composition.values_per_100g["energy_kcal"] = 200.
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="sustainability", score_context=context(flow[0][0]))
    assert result["adapted_recipe"] is None
    assert not flow[1][0]


def test_sustainability_card_retains_full_reference_and_rejects_tampered_factor(flow, references):
    from recipe_wrangler.services.adaptation.models import ComparisonResult
    snapshot = flow[0][0]
    snapshot.ingredients[0].composition.values_per_100g["energy_kcal"] = 200.
    result = pipeline.enhance_recipe(snapshot, title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="sustainability", score_context=context(snapshot))
    objective = NutrientObjective.model_validate(result["objective"])
    checked = ComparisonResult.model_validate(result["comparison"])
    assert objective.impact_context.region == "FR"
    assert all(item.source_reference["source_sha256"] == "fixture-source-hash" for item in objective.impact_context.factors)
    assert objective_delta(checked, objective) < 0
    objective.impact_context.factors[0].value_per_kg += 1.
    assert objective_delta(checked, objective) is None


def test_ten_percent_saving_boundary_is_not_lost_to_float_rounding(flow, references):
    for name, price in [("chicken thigh", .3)] + [(item.provenance["candidate"]["document"], .27) for item in flow[0][1]]:
        references[name] = {"match_status": "matched", "price_scope": "detailed_product", "match_method": "exact_detail:name",
            "matched_product_id": name, "food_category": "meat", "cost_reference_version": "fixture-v1",
            "economic_reference_price_eur_kg": price}
    result = pipeline.enhance_recipe(flow[0][0], title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        mode="cost", score_context=context(flow[0][0]), cost_country="IE")
    assert result["status"] == "draft_requires_review"
    assert result["adapted_recipe"]["impact_estimate"]["after_total"] == pytest.approx(.054)


@pytest.mark.parametrize("region,source", [("IE", "irish"), ("HU", "hungarian"), ("SI", "slovenian"), ("EU", "eu")])
def test_enhance_api_uses_existing_region_mapping_and_new_pipeline(monkeypatch, region, source):
    from recipe_wrangler.services.adaptation.router import router
    from recipe_wrangler.services.adaptation.enhancement_response import finalize_enhancement_response
    run = Mock(return_value=finalize_enhancement_response({"original_recipe_id": "recipe-1", "mode": "cost",
        "status": "needs_evidence", "adapted_recipe": None}))
    monkeypatch.setattr(pipeline, "enhance_active_recipe", run)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/v1/recipes/recipe-1/adapt/enhance", json={"region": region, "mode": "cost"})
    assert response.status_code == 200
    assert run.call_args.args == ("recipe-1", source)
    assert run.call_args.kwargs["cost_country"] == region


def test_public_api_cannot_supply_internal_quantity_or_score_attestations(monkeypatch):
    from recipe_wrangler.services.adaptation.router import router
    run = Mock()
    monkeypatch.setattr(pipeline, "enhance_active_recipe", run)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/v1/recipes/recipe-1/adapt/enhance", json={"region": "IE", "quantity_declarations": []})
    assert response.status_code == 422
    run.assert_not_called()
