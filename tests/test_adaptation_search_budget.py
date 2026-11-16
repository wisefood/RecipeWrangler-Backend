"""Eligibility and deterministic fallback must preserve a three-request ceiling."""

from copy import deepcopy
import json
from unittest.mock import Mock

import openai
import pytest

from test_adaptation_preview import inputs, composition
from test_adaptation_enhancement import breakdown
from test_adaptation_objective_approval import context
from recipe_wrangler.services.adaptation import candidate_selection, contextual_assessment, instruction_editor
from recipe_wrangler.services.adaptation import enhancement_pipeline as pipeline
from recipe_wrangler.services.adaptation.models import RoleAssessment
from recipe_wrangler.services.adaptation.models import InstructionDraft, InstructionPatchPlan


@pytest.fixture
def search(inputs, monkeypatch):
    monkeypatch.setattr(pipeline.matching_store, "food_part_composition_records", lambda *a, **kw: [])
    snapshot = inputs[0]

    def food(identifier, identity, energy):
        result = composition(identifier, "leg", energy)
        result.properties["base_identity"].value = identity
        result.properties["biological_source"].value = identity
        result.provenance["candidate"]["document"] = identity
        return result

    snapshot.ingredients[0].composition = food("lamb", "lamb", 600.)
    second = snapshot.ingredients[0].model_copy(deep=True)
    second.position, second.composition = 1, food("beef", "beef", 599.)
    snapshot.ingredients.append(second)
    for item in snapshot.ingredients:
        name = item.composition.provenance["candidate"]["document"]
        item.name, item.original_text = name, "200 g raw boneless skinless " + name + " leg"
        item.observed_properties = deepcopy(item.composition.properties)
    targets = {0: [food("pork", "pork", 50.)], 1: [food("turkey", "turkey", 50.)]}
    by_id = {item.ref.candidate_id: item for rows in targets.values() for item in rows}
    queries, calls, behavior = [], [], {}

    def records(snapshot, position, *, has_eligible=None):
        queries.append(position)
        return [{**item.ref.model_dump(), "candidate": item.provenance["candidate"]} for item in targets[position]], []

    monkeypatch.setattr(candidate_selection, "production_candidate_records", records)
    monkeypatch.setattr(candidate_selection, "_composition", lambda row: by_id[row["candidate_id"]].model_copy(deep=True))

    def request(payload, response_model, config, **kwargs):
        calls.append(deepcopy(payload))
        if behavior.get("provider_error"):
            raise openai.APIConnectionError(request=Mock())
        ctx = payload["context"]
        target = next(item for item in ctx["ingredients"] if item["position"] == ctx["target_position"])
        step = next(item for item in ctx["steps"] if target["name"] in item["text"])
        evidence = [{"kind": "ingredient", "reference_id": "ingredient:" + str(target["position"]), "quote": target["name"]},
            {"kind": "instruction", "reference_id": step["step_id"], "quote": step["text"]}]
        if response_model is RoleAssessment:
            return json.dumps({"status": "inferred", "roles": [{"role": "main_component", "reason": "Fixture role", "evidence": evidence}], "uncertainties": []})
        if response_model is InstructionPatchPlan:
            if behavior.get("editor_uncertain"):
                return json.dumps({"status": "uncertain", "patches": [], "uncertainties": ["Fixture uncertainty"]})
            return json.dumps({"status": "proposed", "patches": payload["allowed_patches"], "uncertainties": []})
        return json.dumps({"assessments": [{"candidate_id": item["candidate_id"],
            "verdict": "unsuitable" if behavior.get("unsuitable") else "suitable", "reason": "Fixture verdict",
            "role_fit": [{"role": "main_component", "status": "preserved"}], "evidence": evidence,
            "required_changes": [], "uncertainties": []} for item in payload["candidates"]]})

    for module in (contextual_assessment, instruction_editor):
        monkeypatch.setattr(module, "_llm_config", lambda: {"source": "vllm", "model": "mock-search"})
        monkeypatch.setattr(module, "_request", request)
    return snapshot, targets, queries, calls, behavior, by_id


def run(search):
    snapshot = search[0]
    score = context(snapshot)
    score.total_weight_g = 400.
    return pipeline.enhance_recipe(snapshot, title="Lamb and beef dish",
        instructions=["Cook the lamb gently.", "Cook the beef gently."],
        nutri_score_breakdown=breakdown(energy=7), score_context=score)


@pytest.mark.parametrize("failure", ["nutrition", "instruction"])
def test_invalid_first_target_falls_through_before_spending_calls(search, failure):
    target = search[1][0][0]
    if failure == "nutrition":
        target.values_per_100g["sodium_mg"] = 1000.
    else:
        target.properties["food_part"].value = "shoulder"
    result = run(search)
    assert result["status"] == "draft_requires_review", result["reasons"]
    assert search[2] == [0, 1]
    assert result["final_decision"]["target_position"] == 1
    assert len(search[3]) == len(result["model_calls"]) == result["model_call_limit"] == 3
    assert all(item["target_position"] == 1 for item in result["model_calls"])
    first = next(item for item in result["candidates"] if item["target_position"] == 0)
    assert first["culinary_status"] == "not_evaluated"
    assert first["decision_reasons"]


def test_culinary_rejection_does_not_start_a_fresh_target_over_budget(search):
    search[4]["unsuitable"] = True
    result = run(search)
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None
    assert "model_call_budget_prevents_target_fallback" in result["reasons"]
    assert len(result["model_calls"]) == len(search[3]) == 2
    assert search[2] == [0]
    assert result["candidates"][0]["culinary_status"] == "unsuitable"


def test_editor_uncertainty_cannot_trigger_a_fourth_call(search):
    search[4]["editor_uncertain"] = True
    result = run(search)
    assert result["status"] == "needs_evidence" and result["adapted_recipe"] is None
    assert "model_call_budget_exhausted" in result["reasons"]
    assert len(result["model_calls"]) == len(search[3]) == 3
    assert search[2] == [0]
    assert result["candidates"][0]["instruction_decision"]["reasons"] == ["instruction_edit_uncertain"]


def test_provider_failure_is_reported_and_never_retried(search):
    search[4]["provider_error"] = True
    result = run(search)
    assert result["status"] == "unavailable" and result["adapted_recipe"] is None
    assert len(result["model_calls"]) == len(search[3]) == 1
    assert result["reasons"] == ["hosted_provider_unreachable"]
    assert 'could not be reached' in result['explanation']


def test_local_editor_refusal_can_use_another_candidate_without_an_extra_call(search, monkeypatch):
    other = search[1][1][0].model_copy(deep=True)
    other.ref.candidate_id = "other-turkey"
    other.values_per_100g["energy_kcal"] = 100.
    search[1][0].append(other)
    search[5][other.ref.candidate_id] = other
    actual = pipeline.draft_raw_part_instructions
    attempts = []

    def edit(snapshot, preview, candidate_id, **kwargs):
        attempts.append(candidate_id)
        if len(attempts) == 1:
            return InstructionDraft(status="needs_evidence", recipe_id=snapshot.recipe_id,
                baseline_run_id=snapshot.run_id, quantity_input_hash=snapshot.quantity_input_hash,
                candidate_id=candidate_id, reasons=["fixture_local_refusal"])
        return actual(snapshot, preview, candidate_id, **kwargs)

    monkeypatch.setattr(pipeline, "draft_raw_part_instructions", edit)
    result = run(search)
    assert result["status"] == "draft_requires_review", result["reasons"]
    assert len(attempts) == 2 and len(search[3]) == len(result["model_calls"]) == 3
    first = next(item for item in result["candidates"] if item["candidate_id"] == attempts[0])
    assert first["instruction_decision"]["reasons"] == ["fixture_local_refusal"]
    assert first["system_decision"] == "needs_evidence"
    assert result["final_decision"]["selected_candidate_id"] == attempts[1]
