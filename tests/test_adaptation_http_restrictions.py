"""Exercise the real HTTP enhancement pipeline; only data reads and models are mocked."""
from copy import deepcopy
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_adaptation_preview import inputs, model
from test_adaptation_instruction_editor import setup
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement_pipeline import flow, active
from test_adaptation_production_integration import profile
from recipe_wrangler.services.adaptation.router import router
from recipe_wrangler.services.adaptation.models import PropertyAssertion
from recipe_wrangler.services.adaptation import enhancement_pipeline
from recipe_wrangler.services.adaptation.nutrition_benefit import DEFAULT_POLICY, STRICT_POLICY, WITHIN_GRADE_POLICY


def stored_profile(snapshot):
    # Build saved totals for all retained ingredients, not only the edited line.
    normalized = snapshot.model_copy(deep=True)
    mass = sum(i.quantity.weight_g for i in snapshot.ingredients)
    first = normalized.ingredients[0]
    first.quantity.weight_g = mass
    first.composition.values_per_100g = {
        metric: sum(i.quantity.weight_g * i.composition.values_per_100g[metric]
                    for i in snapshot.ingredients) / mass
        for metric in first.composition.values_per_100g
    }
    return profile(normalized)


@pytest.fixture(params=[DEFAULT_POLICY,STRICT_POLICY,WITHIN_GRADE_POLICY])
def http_case(flow, active,monkeypatch,request):
    snapshot, targets = flow[0]
    for composition in [snapshot.ingredients[0].composition, *targets]:
        composition.allergen_assessments["milk"] = PropertyAssertion(
            value="absent", evidence=composition.basis.evidence)
    active[2].side_effect = lambda *args, **kwargs: {"run_id":snapshot.run_id, "profile":stored_profile(snapshot)}
    app = FastAPI()
    app.include_router(router)
    app.state.nutrition_policy=request.param
    if request.param!=DEFAULT_POLICY:
        # Historical policies require internal opt-in; default HTTP uses no override.
        monkeypatch.setattr(enhancement_pipeline,"enhance_active_recipe",
            partial(enhancement_pipeline.enhance_active_recipe,nutrition_policy=request.param))
    with TestClient(app) as client:
        yield client, snapshot, targets, active[0], flow


def request(client, constraints=None):
    return client.post("/api/v1/recipes/recipe-1/adapt/enhance", json={
        "region":"IE", "mode":"nutrition",
        "goal_nutrients":["energy"],
        "constraints":constraints or {"forbidden_allergens":["milk"]},
    })


def test_http_returns_checked_unsaved_draft_when_restriction_evidence_is_stated(http_case):
    client, snapshot, targets, recipe, flow = http_case
    before = deepcopy(snapshot.model_dump())
    response = request(client)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "draft_requires_review", result
    assert result["adapted_recipe"]["instructions"] == ["Cook the chicken breast gently."]
    assert result["constraints"]["forbidden_allergens"] == ["milk"]
    assert len(result["model_calls"]) == 3
    assert result["saved"] is result["recipe_ready"] is False
    assert snapshot.model_dump() == before


def test_http_default_returns_same_grade_draft_and_discloses_benefit(http_case):
    client,snapshot,targets,recipe,flow=http_case
    snapshot.ingredients[0].composition.values_per_100g["energy_kcal"]=200.
    before=deepcopy(snapshot.model_dump())
    result=request(client).json()
    if client.app.state.nutrition_policy==STRICT_POLICY:
        assert result["adapted_recipe"] is None and not result["model_calls"]
    else:
        assert result["status"]=="draft_requires_review",result
        approval=result["final_objective_approval"]
        assert approval["benefit_policy"]==client.app.state.nutrition_policy
        assert approval["before"]["nutri_score"]==approval["after"]["nutri_score"]=="Nutriscore_A"
        assert approval["target_benefit"]["reduction_per_serving"]==100
        assert approval["target_benefit"]["before_per_serving"]==200
        assert approval["target_benefit"]["after_per_serving"]==100
        assert approval["target_benefit"]["relative_reduction"]==0.5
        assert approval["finished_recipe_score"] is False
        assert result["adapted_recipe"]["instructions"]==["Cook the chicken breast gently."]
        assert result["adapted_recipe"]["ingredients"][0]["quantity"]["weight_g"]==200
        assert result["adapted_recipe"]["serves"]==snapshot.serves
        assert result["adapted_recipe"]["nutrition"]["nutri_score"] is None
        assert any("provisional thresholds" in text for text in result["uncertainties"])
        assert len(result["model_calls"])==3
        assert not result["saved"] and not result["recipe_ready"]
    assert snapshot.model_dump()==before


def test_http_default_button_needs_no_goal_or_policy_override(http_case):
    client,snapshot,*_=http_case
    if client.app.state.nutrition_policy!=DEFAULT_POLICY:
        return
    snapshot.ingredients[0].composition.values_per_100g["energy_kcal"]=200.
    result=client.post("/api/v1/recipes/recipe-1/adapt/enhance",json={"region":"IE"}).json()
    assert result["status"]=="draft_requires_review",result
    assert result["final_objective_approval"]["benefit_policy"]==DEFAULT_POLICY
    assert result["final_objective_approval"]["before"]["nutri_score"]==result["final_objective_approval"]["after"]["nutri_score"]
    assert not result["saved"] and not result["recipe_ready"]


def test_http_cannot_enable_pilot_through_a_request_field(http_case):
    client,*_=http_case
    response=client.post("/api/v1/recipes/recipe-1/adapt/enhance",json={
        "region":"IE","mode":"nutrition","nutrition_policy":WITHIN_GRADE_POLICY})
    assert response.status_code==422


@pytest.mark.parametrize("evidence", ["missing", "inferred_present", "stated_present", "possible"])
def test_http_never_converts_missing_or_conflicting_replacement_evidence_to_absence(http_case, evidence):
    client, snapshot, targets, recipe, flow = http_case
    for target in targets:
        assertion = target.allergen_assessments["milk"]
        if evidence == "missing": target.allergen_assessments.clear()
        elif evidence == "possible": assertion.value = "unknown"
        else:
            assertion.value = "present"
            if evidence == "inferred_present": assertion.evidence.status = "inferred"
    response = request(client)
    assert response.status_code == 200
    result = response.json()
    assert result["adapted_recipe"] is None and result["status"] != "draft_requires_review"
    assert not result["model_calls"] and not flow[1][0] and not flow[2][2]
    assert result["constraints"]["forbidden_allergens"] == ["milk"]


@pytest.mark.parametrize("original", ["rice with milk", "rice (optional milk)", "rice or milk"])
def test_http_checks_retained_originals_even_when_selected_composition_claims_absence(http_case, original):
    client, snapshot, targets, recipe, flow = http_case
    retained = snapshot.ingredients[0].model_copy(deep=True)
    retained.position, retained.name, retained.original_text = 1, "rice", original
    retained.quantity.weight_g = 1.
    snapshot.ingredients.append(retained)
    recipe["original_ingredients"].append(original)
    response = request(client)
    assert response.status_code == 200
    result = response.json()
    assert result["adapted_recipe"] is None and result["status"] != "draft_requires_review"
    assert not result["model_calls"]
    assert "source_text_restriction:allergen:milk" in str(result)


@pytest.mark.parametrize("location", ["title", "ingredient", "instruction"])
def test_http_final_text_cannot_restore_a_forbidden_ingredient(http_case, location):
    client, snapshot, targets, recipe, flow = http_case
    if location == "title": recipe["title"] += " with cream"
    elif location == "ingredient":
        snapshot.ingredients[0].original_text += " with cream"
        recipe["original_ingredients"][0] = snapshot.ingredients[0].original_text
    else: recipe["instructions"][0] += " Serve with cream."
    response = request(client)
    assert response.status_code == 200
    result = response.json()
    assert result["adapted_recipe"] is None and result["status"] != "draft_requires_review"
    assert "allergen:milk" in str(result)


def test_http_required_vegan_diet_rejects_an_animal_replacement_before_inference(http_case):
    client, snapshot, targets, recipe, flow = http_case
    response = request(client, {"required_consumer_groups":["vegan"]})
    assert response.status_code == 200
    result = response.json()
    assert result["adapted_recipe"] is None
    assert not result["model_calls"]
    assert "consumer_group_conflict:vegan" in str(result)


def test_http_does_not_advertise_unsupported_pescatarian_enhancement(http_case):
    client, *_ = http_case
    response = request(client, {"required_consumer_groups":["pescatarian"]})
    assert response.status_code == 422
