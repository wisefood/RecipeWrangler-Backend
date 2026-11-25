"""Mocked model responses test contracts and orchestration, not culinary accuracy."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import openai
import pytest
from pydantic import ValidationError

from recipe_wrangler.services.adaptation import contextual_assessment as service
from recipe_wrangler.services.adaptation.models import ComparisonConstraints, ComparisonSnapshot, RawPartEdit
from recipe_wrangler.services.adaptation.models import ContextCandidate


def evidence():
    return {"reference": "synthetic-constraint-check", "method": "deterministic_check", "version": "1", "status": "stated"}


@pytest.fixture
def context():
    snapshot = ComparisonSnapshot.model_validate({
        "recipe_id": "recipe-1", "run_id": "run-1", "nutrition_source": "eu", "release_id": "release-1",
        "quantity_input_hash": "quantity-1", "serves": 2,
        "ingredients": [{"position": position, "name": name, "original_text": original,
                         "quantity": {"weight_g": mass, "basis": "unknown", "evidence": evidence()}}
                        for position, name, original, mass in [(0, "egg", "1 raw egg", 50), (1, "beans", "200 g beans", 200)]],
    })
    return service.build_adaptation_context(snapshot, title="Bean burgers",
        instructions=["Mix the egg with the beans.", "Shape the mixture into patties."],
        objective="Replace the egg while preserving the burger structure", target_position=0)


@pytest.fixture
def candidates():
    return [ContextCandidate(candidate_id=name, label=name,
        composition={"release_id": "release-1", "collection": "nutritional_ingredients_eu", "candidate_id": name},
        constraint_status="passed", constraint_evidence=[evidence()]) for name in ("candidate-a", "candidate-b")]


def quotes():
    return [{"kind": "ingredient", "reference_id": "ingredient:0", "quote": "egg"},
            {"kind": "instruction", "reference_id": "step:0", "quote": "Mix the egg with the beans."}]


def planned_edit(context, candidate):
    return RawPartEdit(recipe_id=context.recipe_id, baseline_run_id=context.baseline_run_id,
        source_identity="egg", target_identity=candidate.label,
        quantity_input_hash=context.quantity_input_hash, position=context.target_position,
        from_composition={"release_id": "release-1", "collection": "nutritional_ingredients_eu", "candidate_id": "source"},
        to_composition=candidate.composition)


def role_payload():
    return {"status": "inferred", "roles": [{"role": "binding", "reason": "The egg helps hold the patties together.",
            "evidence": quotes()}], "uncertainties": []}


def test_roles_can_share_grounded_context_evidence(context):
    payload = role_payload()
    payload['roles'][0]['evidence'] = [quotes()[1]]
    payload['roles'].append({'role': 'flavour', 'reason': 'Ingredient contributes flavour.',
                             'evidence': [quotes()[0]]})
    assessment = service.RoleAssessment.model_validate(payload)
    service._check_roles(assessment, context)
    assessment.roles[1].evidence[0].quote = 'invented ingredient'
    with pytest.raises(ValueError, match='unsupported_context_quote'):
        service._check_roles(assessment, context)


@pytest.mark.parametrize('evidence_index,reason', [(0, 'instruction_evidence_required')])
def test_roles_still_require_both_context_sources(context, evidence_index, reason):
    payload = role_payload()
    payload['roles'][0]['evidence'] = [quotes()[evidence_index]]
    with pytest.raises(ValueError, match=reason):
        service._check_roles(service.RoleAssessment.model_validate(payload), context)


def test_unique_target_named_in_exact_instruction_can_ground_role(context):
    payload = role_payload()
    payload['roles'][0]['evidence'] = [quotes()[1]]
    service._check_roles(service.RoleAssessment.model_validate(payload), context)


def test_instruction_only_role_needs_unambiguous_target_name(context):
    payload = role_payload()
    payload['roles'][0]['evidence'] = [{'kind': 'instruction', 'reference_id': 'step:1',
                                       'quote': 'Shape the mixture into patties.'}]
    with pytest.raises(ValueError, match='target_occurrence_evidence_required'):
        service._check_roles(service.RoleAssessment.model_validate(payload), context)
    payload['roles'][0]['evidence'] = [quotes()[1]]
    context.ingredients.append(context.ingredients[0].model_copy(deep=True, update={'position': 2}))
    with pytest.raises(ValueError, match='target_occurrence_evidence_required'):
        service._check_roles(service.RoleAssessment.model_validate(payload), context)


def batch_payload():
    return {"assessments": [{"candidate_id": name, "verdict": "suitable", "reason": "Synthetic suitability assessment.",
        "role_fit": [{"role": "binding", "status": "preserved"}], "evidence": quotes(),
        "required_changes": [], "uncertainties": []} for name in ("candidate-a", "candidate-b")]}


@pytest.fixture
def model(monkeypatch):
    config = {"source": "vllm", "model": "test-model", "base_url": "http://unused.invalid/v1",
              "api_key": "test-secret-never-in-prompt", "timeout": 1}
    monkeypatch.setattr(service, "_llm_config", lambda: config)
    create = Mock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    handle = MagicMock()
    handle.__enter__.return_value = client
    factory = Mock(return_value=handle)
    monkeypatch.setattr(service.openai, "OpenAI", factory)
    def respond(*payloads):
        create.side_effect = [payload if isinstance(payload, Exception) else SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(payload) if not isinstance(payload, str) else payload)
        )]) for payload in payloads]
        return create
    return respond, create, factory


def test_infer_once_then_assess_all_candidates_in_one_batch(context, candidates, model):
    respond, create, factory = model
    respond(role_payload(), batch_payload())
    before_context, before_candidates = context.model_dump(), [item.model_dump() for item in candidates]
    roles = service.infer_contextual_roles(context)
    result = service.assess_contextual_candidates(context, roles, candidates)
    assert create.call_count == 2 and factory.call_count == 2
    assert factory.return_value.__exit__.call_count == 2
    role_input = json.loads(create.call_args_list[0].kwargs["messages"][1]["content"])
    batch_input = json.loads(create.call_args_list[1].kwargs["messages"][1]["content"])
    assert "candidates" not in role_input
    assert role_input["context"]["target_position"] == 0
    assert batch_input["context"]["steps"][0]["text"] == "Mix the egg with the beans."
    assert len(batch_input["candidates"]) == 2 and batch_input["roles"]["roles"][0]["role"] == "binding"
    assert result.status == "assessed" and len(result.assessments) == 2
    assert roles.evidence_status == result.evidence_status == "inferred"
    assert result.numerical_approval is False and result.instruction_status == "pending"
    assert context.model_dump() == before_context and [item.model_dump() for item in candidates] == before_candidates
    assert "test-secret-never-in-prompt" not in json.dumps(batch_input) + result.model_dump_json()
    factory.assert_called_with(base_url="http://unused.invalid/v1", api_key="test-secret-never-in-prompt", timeout=1, max_retries=0)


def test_established_roles_can_be_reused_for_another_batch(context, candidates, model):
    respond, create, factory = model
    respond(role_payload(), batch_payload(), batch_payload())
    roles = service.infer_contextual_roles(context)
    first = service.assess_contextual_candidates(context, roles, candidates)
    second = service.assess_contextual_candidates(context, roles, candidates)
    assert create.call_count == 3
    assert first.role_assessment_hash == second.role_assessment_hash == roles.assessment_hash


@pytest.mark.parametrize("verdict", ["unsuitable", "uncertain"])
def test_all_rejected_or_uncertain_never_falls_back_to_acceptance(context, candidates, model, verdict):
    respond, create, factory = model
    batch = batch_payload()
    for item in batch["assessments"]:
        item["verdict"] = verdict
        item["role_fit"][0]["status"] = "not_preserved" if verdict == "unsuitable" else "uncertain"
        item["uncertainties"] = ["Insufficient functional evidence."] if verdict == "uncertain" else []
    respond(role_payload(), batch)
    result = service.assess_contextual_candidates(context, service.infer_contextual_roles(context), candidates)
    assert len(result.assessments) == 2
    assert all(item.verdict == verdict for item in result.assessments)
    assert result.status == ("uncertain" if verdict == "uncertain" else "assessed")


def test_required_changes_remain_unexecuted_proposals_with_existing_targets(context, candidates, model):
    respond, create, factory = model
    batch = batch_payload()
    batch["assessments"][0].update(verdict="suitable_with_changes", required_changes=[{
        "kind": "instruction", "ingredient_positions": [0], "step_ids": ["step:0"],
        "description": "Prepare the replacement before mixing it into the beans.",
    }])
    respond(role_payload(), batch)
    result = service.assess_contextual_candidates(context, service.infer_contextual_roles(context), candidates)
    assert result.assessments[0].required_changes[0].status == "proposed_requires_validation"
    assert context.steps[0].text == "Mix the egg with the beans."
    assert result.numerical_approval is False


@pytest.mark.parametrize("change", ["missing_candidate", "unknown_candidate", "duplicate_candidate", "missing_role",
                                    "new_role", "unknown_step", "invented_quote", "wrong_occurrence", "metric",
                                    "quantity_declaration", "undeclared_change", "missing_changes", "unknown_change_target"])
def test_bad_batch_responses_are_unavailable_without_partial_approval(context, candidates, model, change):
    respond, create, factory = model
    batch = batch_payload()
    item = batch["assessments"][0]
    if change == "missing_candidate": batch["assessments"].pop()
    elif change == "unknown_candidate": item["candidate_id"] = "invented"
    elif change == "duplicate_candidate": batch["assessments"][1]["candidate_id"] = item["candidate_id"]
    elif change == "missing_role": item["role_fit"] = []
    elif change == "new_role": item["role_fit"].append({"role": "flavour", "status": "preserved"})
    elif change == "unknown_step": item["evidence"][1]["reference_id"] = "step:99"
    elif change == "invented_quote": item["evidence"][1]["quote"] = "The source never says this."
    elif change == "wrong_occurrence": item["evidence"][0] = {"kind": "ingredient", "reference_id": "ingredient:1", "quote": "beans"}
    elif change == "metric": item["calculated_energy_kcal"] = 50
    elif change == "quantity_declaration": item["quantity_declarations"] = [{"basis": "raw_edible"}]
    elif change == "undeclared_change": item["role_fit"][0]["status"] = "needs_changes"
    elif change == "missing_changes": item["verdict"] = "suitable_with_changes"
    else:
        item.update(verdict="suitable_with_changes", required_changes=[{"kind": "ingredient", "ingredient_positions": [99],
            "step_ids": ["step:0"], "description": "Change an ingredient."}])
    respond(role_payload(), batch)
    result = service.assess_contextual_candidates(context, service.infer_contextual_roles(context), candidates)
    assert result.status == "unavailable"
    assert result.failure_reason == "candidate_assessment_unavailable_or_invalid"
    assert result.assessments == []


@pytest.mark.parametrize("change", ["invented_quote", "wrong_occurrence", "missing_instruction", "unknown_role",
                                    "quantity_declaration", "observed_role"])
def test_invalid_roles_cannot_reach_candidate_evaluation(context, candidates, model, change):
    respond, create, factory = model
    payload = role_payload()
    role = payload["roles"][0]
    if change == "invented_quote": role["evidence"][1]["quote"] = "invented instruction"
    elif change == "wrong_occurrence": role["evidence"][0] = {"kind": "ingredient", "reference_id": "ingredient:1", "quote": "beans"}
    elif change == "missing_instruction": role["evidence"].pop()
    elif change == "unknown_role": role["role"] = "universal_egg_capability"
    elif change == "quantity_declaration": payload["quantity_declarations"] = []
    else: payload["status"] = "stated"
    respond(payload)
    roles = service.infer_contextual_roles(context)
    assert roles.status == "unavailable"
    batch = service.assess_contextual_candidates(context, roles, candidates)
    assert batch.status == "uncertain" and not batch.assessments
    assert create.call_count == 1


@pytest.mark.parametrize("missing_steps", [False, True])
def test_uncertain_or_missing_context_does_not_trigger_a_candidate_call(context, candidates, model, missing_steps):
    respond, create, factory = model
    if missing_steps:
        context.steps = []
    respond({"status": "uncertain", "roles": [], "uncertainties": ["Cannot establish the ingredient's function."]})
    roles = service.infer_contextual_roles(context)
    batch = service.assess_contextual_candidates(context, roles, candidates)
    assert roles.status == batch.status == "uncertain"
    assert batch.assessments == []
    assert create.call_count == (0 if missing_steps else 1)


@pytest.mark.parametrize("changed", ["instructions", "run", "target", "role"])
def test_role_results_are_bound_to_context_and_cannot_be_reused_after_changes(context, candidates, model, changed):
    respond, create, factory = model
    respond(role_payload())
    roles = service.infer_contextual_roles(context)
    if changed == "instructions": context.steps[0].text = "Do something else."
    elif changed == "run": context.baseline_run_id = "new-run"
    elif changed == "target": context.target_position = 1
    else: roles.assessment.roles[0].reason = "altered assessment"
    result = service.assess_contextual_candidates(context, roles, candidates)
    assert result.status == "unavailable" and result.failure_reason == "role_context_mismatch_or_invalid"
    assert create.call_count == 1


def test_hard_constraint_exclusions_are_applied_before_the_batch_call(context, candidates, model):
    respond, create, factory = model
    rejected = candidates[1].model_copy(deep=True)
    rejected.constraint_status, rejected.constraint_reasons = "blocked", ["forbidden_allergen"]
    batch = batch_payload()
    batch["assessments"].pop()
    respond(role_payload(), batch)
    result = service.assess_contextual_candidates(context, service.infer_contextual_roles(context), [candidates[0], rejected])
    assert [item.candidate_id for item in result.excluded_candidates] == ["candidate-b"]
    sent = json.loads(create.call_args.kwargs["messages"][1]["content"])["candidates"]
    assert [item["candidate_id"] for item in sent] == ["candidate-a"]


@pytest.mark.parametrize("status", ["blocked", "unknown"])
def test_all_excluded_candidates_need_no_model_call(context, candidates, model, status):
    respond, create, factory = model
    respond(role_payload())
    roles = service.infer_contextual_roles(context)
    for item in candidates:
        item.constraint_status, item.constraint_reasons = status, ["not_verified"]
    result = service.assess_contextual_candidates(context, roles, candidates)
    assert not result.assessments and len(result.excluded_candidates) == 2
    assert create.call_count == 1


@pytest.mark.parametrize("stage", ["roles", "batch"])
@pytest.mark.parametrize("response", ["timeout", "bad_json", "truncated"])
def test_provider_failures_never_produce_approval(context, candidates, model, stage, response):
    respond, create, factory = model
    invalid = openai.APITimeoutError(request=Mock()) if response == "timeout" else "not JSON"
    respond(*([role_payload()] if stage == "batch" else []), invalid)
    if response == "truncated":
        values = list(create.side_effect)
        values[-1] = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{}"))])
        create.side_effect = values
    roles = service.infer_contextual_roles(context)
    result = service.assess_contextual_candidates(context, roles, candidates) if stage == "batch" else roles
    assert result.status == "unavailable"
    assert result.numerical_approval is False


def test_request_models_reject_ambiguous_occurrences_and_unproven_constraints(context, candidates):
    data = context.model_dump()
    data["ingredients"].append(deepcopy(data["ingredients"][0]))
    with pytest.raises(ValidationError, match="unique"):
        type(context).model_validate(data)
    data = candidates[0].model_dump()
    data["constraint_evidence"] = []
    with pytest.raises(ValidationError):
        ContextCandidate.model_validate(data)


def test_context_can_infer_different_roles_for_the_same_ingredient_in_different_recipes(context, model):
    respond, create, factory = model
    burger = role_payload()
    omelette = role_payload()
    omelette["roles"][0]["role"] = "main_component"
    omelette["roles"][0]["evidence"][1]["quote"] = "Cook the beaten egg."
    respond(burger, omelette)
    first = service.infer_contextual_roles(context)
    context.title, context.steps[0].text = "Omelette", "Cook the beaten egg."
    second = service.infer_contextual_roles(context)
    assert first.assessment.roles[0].role == "binding"
    assert second.assessment.roles[0].role == "main_component"
    assert first.context_hash != second.context_hash


def test_constraints_are_passed_as_data_and_changed_constraints_invalidate_roles(context, candidates, model):
    respond, create, factory = model
    context.constraints = ComparisonConstraints(forbidden_allergens={"milk"})
    respond(role_payload(), batch_payload())
    roles = service.infer_contextual_roles(context)
    result = service.assess_contextual_candidates(context, roles, candidates)
    assert result.status == "assessed"
    packet = json.loads(create.call_args.kwargs["messages"][1]["content"])
    assert packet["context"]["constraints"]["forbidden_allergens"] == ["milk"]
    context.constraints.forbidden_allergens.add("soy")
    assert service.assess_contextual_candidates(context, roles, candidates).status == "unavailable"
    assert create.call_count == 2


@pytest.mark.parametrize("reply", ['{"status":"inferred","status":"uncertain","roles":[],"uncertainties":["x"]}',
                                  '```json\n{}\n```', '{} trailing text'])
def test_duplicate_fields_and_partial_json_are_not_silently_recovered(context, model, reply):
    respond, create, factory = model
    respond(reply)
    assert service.infer_contextual_roles(context).status == "unavailable"


@pytest.mark.parametrize("change", [
    {"kind": "quantity", "ingredient_positions": []}, {"ingredient_positions": [0, 0]},
    {"step_ids": ["step:0", "step:0"]}, {"status": "approved"}, {"weight_g": 200},
])
def test_required_changes_cannot_smuggle_in_executable_edits(change):
    from recipe_wrangler.services.adaptation.models import RequiredContextChange
    data = {"kind": "instruction", "ingredient_positions": [0], "step_ids": ["step:0"], "description": "Prepare differently."}
    with pytest.raises(ValidationError):
        RequiredContextChange.model_validate({**data, **change})


def test_invalid_candidate_sets_are_rejected_before_another_model_call(context, candidates, model):
    respond, create, factory = model
    respond(role_payload())
    roles = service.infer_contextual_roles(context)
    with pytest.raises(ValueError, match="unique"):
        service.assess_contextual_candidates(context, roles, [candidates[0], candidates[0]])
    with pytest.raises(ValueError, match="ten"):
        service.assess_contextual_candidates(context, roles, [candidates[0].model_copy(update={"candidate_id": str(i)}) for i in range(11)])
    assert create.call_count == 1


def test_candidate_results_pin_exact_compositions_and_constraint_evidence(context, candidates, model):
    respond, create, factory = model
    respond(role_payload(), batch_payload(), batch_payload())
    roles = service.infer_contextual_roles(context)
    first = service.assess_contextual_candidates(context, roles, candidates)
    candidates[0].composition.release_id = "different-release"
    second = service.assess_contextual_candidates(context, roles, candidates)
    assert first.context_hash == second.context_hash
    assert first.candidate_batch_hash != second.candidate_batch_hash


def test_constraint_sets_have_a_canonical_order_in_prompts(context, model):
    respond, create, factory = model
    context.constraints.forbidden_allergens = {"soy", "milk", "nuts", "egg"}
    respond(role_payload())
    service.infer_contextual_roles(context)
    packet = json.loads(create.call_args.kwargs["messages"][1]["content"])
    assert packet["context"]["constraints"]["forbidden_allergens"] == ["egg", "milk", "nuts", "soy"]


def test_planned_operation_is_sent_and_bound_to_candidate_hash(context, candidates, model):
    respond, create, factory = model
    candidates[0].planned_edit = planned_edit(context, candidates[0])
    respond(role_payload(), batch_payload(), batch_payload())
    roles = service.infer_contextual_roles(context)
    first = service.assess_contextual_candidates(context, roles, candidates)
    packet = json.loads(create.call_args.kwargs["messages"][1]["content"])
    assert packet["candidates"][0]["planned_edit"] == candidates[0].planned_edit.model_dump(mode="json")
    assert "beyond that operation" in packet["task"]
    assert first.prompt_version == "culinary_context_v3"
    candidates[0].planned_edit.from_composition.candidate_id = "different-source"
    second = service.assess_contextual_candidates(context, roles, candidates)
    assert first.candidate_batch_hash != second.candidate_batch_hash


@pytest.mark.parametrize("field,value", [("recipe_id", "other"), ("baseline_run_id", "other"),
                                       ("quantity_input_hash", "other"), ("position", 1)])
def test_planned_operation_scope_mismatch_never_calls_model(context, candidates, model, field, value):
    respond, create, factory = model
    respond(role_payload())
    roles = service.infer_contextual_roles(context)
    candidates[0].planned_edit = planned_edit(context, candidates[0])
    setattr(candidates[0].planned_edit, field, value)
    with pytest.raises(ValueError, match="target recipe context"):
        service.assess_contextual_candidates(context, roles, candidates)
    assert create.call_count == 1


def test_planned_operation_cannot_target_another_candidate(context, candidates):
    candidate = candidates[0].model_dump()
    candidate["planned_edit"] = planned_edit(context, candidates[1]).model_dump()
    with pytest.raises(ValidationError, match="exact candidate"):
        ContextCandidate.model_validate(candidate)


def test_genuine_additional_cooking_changes_are_not_automatically_accepted(context, candidates, model):
    respond, create, factory = model
    for candidate in candidates: candidate.planned_edit = planned_edit(context, candidate)
    batch = batch_payload()
    for item in batch["assessments"]:
        item["verdict"] = "suitable_with_changes"
        item["role_fit"][0]["status"] = "needs_changes"
        item["required_changes"] = [{"kind": "cooking_method", "ingredient_positions": [0],
            "step_ids": ["step:0"], "description": "Needs a separately validated cooking adjustment."}]
    respond(role_payload(), batch)
    roles = service.infer_contextual_roles(context)
    result = service.assess_contextual_candidates(context, roles, candidates)
    assert all(item.verdict == "suitable_with_changes" and item.required_changes for item in result.assessments)


def test_absent_source_overlay_preserves_existing_context_payload(context):
    payload = service._context_payload(context)
    assert all("source_evidence" not in item for item in payload["ingredients"])
