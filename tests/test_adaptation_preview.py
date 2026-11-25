"""Synthetic nutrition and mocked culinary responses exercise the joined workflow."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from recipe_wrangler.services.adaptation import contextual_assessment as culinary
from recipe_wrangler.services.adaptation import preview as service
from test_adaptation_comparison import RAW_CHICKEN_PROPERTIES
from recipe_wrangler.services.adaptation.models import (
    ComparisonComposition, ComparisonConstraints, ComparisonSnapshot, QuantityBasisDeclaration,
)
from recipe_wrangler.services.adaptation.comparison_store import ComparisonLoadError
from recipe_wrangler.services.adaptation.models import RoleAssessment
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.tools.nutrition_completeness import METRICS


def evidence():
    return {"reference": "synthetic-fixture", "method": "synthetic-review", "version": "1", "status": "stated"}


def composition(identifier, part, energy, protein=20):
    values = dict.fromkeys(METRICS, 0.0)
    values.update(energy_kcal=energy, protein_g=protein)
    return ComparisonComposition.model_validate({
        "ref": {"release_id": "release-1", "collection": "nutritional_ingredients_eu", "candidate_id": identifier},
        "source_dataset": "synthetic-table", "source_release": "1",
        "basis": {"amount_g": 100, "portion": "edible", "evidence": evidence()},
        "properties": {key: {"value": value, "evidence": evidence()} for key, value in {
            **RAW_CHICKEN_PROPERTIES, "food_part": part}.items()},
        "values_per_100g": values, "nutrient_definitions": dict.fromkeys(METRICS, "synthetic-definition"),
        "provenance": {"candidate": {"document": f"Chicken {part} {identifier}"}},
    })


@pytest.fixture
def inputs():
    source = composition("thigh", "thigh", 200)
    snapshot = ComparisonSnapshot.model_validate({
        "recipe_id": "recipe-1", "run_id": "run-1", "nutrition_source": "irish", "release_id": "release-1",
        "quantity_input_hash": "quantity-1", "serves": 2,
        "ingredients": [{"position": 0, "name": "chicken thigh", "original_text": "200 g raw boneless skinless chicken thigh",
                         "quantity": {"weight_g": 200, "basis": "raw_edible", "evidence": evidence()},
                         "observed_properties": source.properties, "composition": source}],
    })
    return snapshot, [composition("breast-a", "breast", 140, 23), composition("breast-b", "breast", 100, 24)]


@pytest.fixture
def model(monkeypatch):
    calls, verdicts = [], {}
    monkeypatch.setattr(culinary, "_llm_config", lambda: {"model": "mock-model", "source": "vllm"})
    def request(payload, response_model, config, **kwargs):
        calls.append(deepcopy(payload))
        quotes = [{"kind": "ingredient", "reference_id": "ingredient:0", "quote": "chicken thigh"},
                  {"kind": "instruction", "reference_id": "step:0", "quote": "Cook the chicken thigh gently."}]
        if response_model is RoleAssessment:
            return json.dumps({"status": "inferred", "roles": [{"role": "main_component", "reason": "Main component of this dish.",
                                                               "evidence": quotes}], "uncertainties": []})
        assessments = []
        for candidate in payload["candidates"]:
            verdict = verdicts.get(candidate["composition"]["candidate_id"], "suitable")
            changes = [{"kind": "quantity", "ingredient_positions": [0], "step_ids": ["step:0"],
                        "description": "Reconsider the replacement amount before proceeding."}] if verdict == "suitable_with_changes" else []
            assessments.append({"candidate_id": candidate["candidate_id"], "verdict": verdict, "reason": "Synthetic culinary assessment.",
                "role_fit": [{"role": "main_component", "status": "uncertain" if verdict == "uncertain" else "preserved"}],
                "evidence": quotes, "required_changes": changes, "uncertainties": ["Feasibility unknown."] if verdict == "uncertain" else []})
        return json.dumps({"assessments": assessments})
    monkeypatch.setattr(culinary, "_request", request)
    return calls, verdicts


def run_preview(inputs, **kwargs):
    snapshot, candidates = inputs
    return service.preview_raw_part_candidates(snapshot, candidates, title="Chicken dish",
        instructions=["Cook the chicken thigh gently."], target_position=0,
        objective=kwargs.pop("objective", NutrientObjective(metric="energy_kcal")), **kwargs)


def test_supported_candidates_rank_by_declared_metric_and_still_require_instruction_editing(inputs, model):
    calls, verdicts = model
    before = [inputs[0].model_dump(), [item.model_dump() for item in inputs[1]]]
    result = run_preview(inputs)
    assert result.status == "has_supported_candidates" and len(calls) == 2
    by_ref = {item.composition.candidate_id: item for item in result.candidates}
    assert by_ref["breast-b"].rank == 1 and by_ref["breast-a"].rank == 2
    assert by_ref["breast-b"].objective_improvement_per_serving == 100
    assert by_ref["breast-a"].objective_improvement_per_serving == 60
    assert all(item.status == "ready_for_instruction_editing" for item in result.candidates)
    assert result.recipe_ready is False and result.instruction_status == "pending"
    assert result.culinary_batch.numerical_approval is False
    assert all(item["planned_edit"]["quantity_policy"] == "preserve_raw_edible_mass" for item in calls[1]["candidates"])
    assert [inputs[0].model_dump(), [item.model_dump() for item in inputs[1]]] == before
    result.model_dump_json()


def test_increase_objective_uses_the_requested_nutrient_not_an_overall_health_claim(inputs, model):
    inputs[1][1].values_per_100g["energy_kcal"] = 250
    result = run_preview(inputs, objective=NutrientObjective(metric="protein_g", direction="increase"))
    best = next(item for item in result.candidates if item.rank == 1)
    assert best.composition.candidate_id == "breast-b"
    assert best.objective_improvement_per_serving == 4
    assert best.comparison.recipe_delta_per_serving["energy_kcal"] == 50


@pytest.mark.parametrize("issue", ["missing_nutrient", "missing_serves", "unresolved_other_ingredient", "allergen", "source_basis"])
def test_incomplete_recipe_or_constraints_never_become_ranked_improvements(inputs, model, issue):
    snapshot, candidates = inputs
    kwargs = {}
    if issue == "missing_nutrient":
        for item in candidates: item.values_per_100g["energy_kcal"] = None
    elif issue == "missing_serves": snapshot.serves = None
    elif issue == "unresolved_other_ingredient":
        other = snapshot.ingredients[0].model_copy(deep=True)
        other.position, other.composition = 1, None
        snapshot.ingredients.append(other)
    elif issue == "allergen": kwargs["constraints"] = ComparisonConstraints(forbidden_allergens={"milk"})
    else:
        for item in candidates: item.basis.evidence.status = "proxy"
    result = run_preview(inputs, **kwargs)
    assert result.status == "needs_evidence"
    assert all(item.rank is None and item.status == "needs_evidence" for item in result.candidates)
    assert model[0] == []


def test_nutritionally_nonimproving_and_incompatible_candidates_are_rejected_before_model_calls(inputs, model):
    inputs[1][0].values_per_100g["energy_kcal"] = 220
    inputs[1][1].properties["qualifiers.added_salt"].value = True
    result = run_preview(inputs)
    assert result.status == "no_supported_candidates"
    assert all(item.status == "rejected" and item.rank is None for item in result.candidates)
    assert model[0] == []


@pytest.mark.parametrize("verdict,expected", [("unsuitable", "no_supported_candidates"),
                                             ("uncertain", "needs_evidence"), ("suitable_with_changes", "changes_required")])
def test_culinary_outcomes_are_preserved_without_approving_additional_operations(inputs, model, verdict, expected):
    for identifier in ("breast-a", "breast-b"): model[1][identifier] = verdict
    result = run_preview(inputs)
    assert result.status == expected
    assert all(item.rank is None for item in result.candidates)
    assert all(item.culinary.verdict == verdict for item in result.candidates)
    if verdict == "suitable_with_changes":
        assert all(item.numerical_scope == "single_variant_edit_only" for item in result.candidates)
        assert result.candidates[0].culinary.required_changes[0].status == "proposed_requires_validation"


def test_only_ready_candidates_rank_even_when_a_change_dependent_candidate_has_a_bigger_numeric_gain(inputs, model):
    model[1]["breast-b"] = "suitable_with_changes"
    result = run_preview(inputs)
    assert result.candidates[0].rank == 1
    assert result.candidates[1].rank is None and result.candidates[1].status == "needs_changes"


def test_model_outage_keeps_known_nutrient_deltas_but_grants_no_readiness(inputs, model, monkeypatch):
    import openai
    monkeypatch.setattr(culinary, "_request", Mock(side_effect=openai.APIConnectionError(request=Mock())))
    result = run_preview(inputs)
    assert result.status == "needs_evidence" and result.roles.status == "unavailable"
    assert all(item.rank is None and item.comparison.recipe_delta_per_serving["energy_kcal"] < 0 for item in result.candidates)


@pytest.mark.parametrize("field", ["context_hash", "candidate_batch_hash", "role_assessment_hash"])
def test_stale_contextual_wrappers_cannot_be_joined_to_fresh_numerical_results(inputs, model, monkeypatch, field):
    original = service.assess_contextual_candidates
    def changed(*args):
        result = original(*args)
        setattr(result, field, "stale")
        return result
    monkeypatch.setattr(service, "assess_contextual_candidates", changed)
    result = run_preview(inputs)
    assert result.status == "needs_evidence" and result.reasons == ["assessment_input_mismatch"]
    assert all(item.rank is None for item in result.candidates)


def test_quantity_declarations_remain_visible_in_the_joined_preview(inputs, model):
    snapshot, candidates = inputs
    snapshot.ingredients[0].quantity.basis = "unknown"
    declaration = QuantityBasisDeclaration(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, position=0, weight_g=200, basis="raw_edible",
        declared_by="synthetic-reviewer", statement="I confirm the saved amount refers to raw edible food.",
        evidence={"reference": "synthetic-declaration", "version": "1", "method": "source_review"})
    result = run_preview(inputs, quantity_declarations=[declaration])
    assert result.status == "has_supported_candidates"
    assert result.candidates[0].comparison.baseline_quantity.basis == "unknown"
    assert result.candidates[0].comparison.quantity_declarations[0].declared_by == "synthetic-reviewer"
    assert snapshot.ingredients[0].quantity.basis == "unknown"


def test_duplicate_or_unbounded_candidate_pools_are_rejected(inputs, model):
    snapshot, candidates = inputs
    with pytest.raises(ValueError, match="unique"):
        run_preview((snapshot, [candidates[0], candidates[0]]))
    with pytest.raises(ValueError, match="ten"):
        run_preview((snapshot, [candidates[0].model_copy(update={"ref": candidates[0].ref.model_copy(update={"candidate_id": str(i)})}) for i in range(11)]))
    assert model[0] == []


@pytest.fixture
def active_store(inputs, monkeypatch):
    snapshot, candidates = inputs
    loader = Mock(return_value=snapshot)
    monkeypatch.setattr(service, "load_active_snapshot", loader)
    lookup = Mock(side_effect=lambda release, collection, *, candidate_id: [
        {"normalized": item} for item in candidates if item.ref.candidate_id == candidate_id])
    monkeypatch.setattr(service.matching_store, "composition_records", lookup)
    monkeypatch.setattr(service, "_composition", lambda row: row["normalized"])
    active = Mock(return_value={"run_id": snapshot.run_id})
    monkeypatch.setattr(service.matching_store, "active_profile", active)
    for name in ("save_run", "insert_run", "persist_profile_record", "enqueue_projection", "latest_profile"):
        monkeypatch.setattr(service.matching_store, name, Mock(side_effect=AssertionError("Unexpected write/fallback")))
    def run(**kwargs):
        return service.preview_active_raw_part_candidates(snapshot.recipe_id, snapshot.nutrition_source,
            [item.ref for item in candidates], baseline_run_id=kwargs.pop("baseline_run_id", snapshot.run_id),
            quantity_input_hash=snapshot.quantity_input_hash, title="Chicken dish",
            instructions=["Cook the chicken thigh gently."], target_position=0,
            objective=NutrientObjective(metric="energy_kcal"), **kwargs)
    return run, loader, lookup, active


def test_read_only_entry_point_loads_exact_candidates_and_rechecks_active_run(active_store, model):
    run, loader, lookup, active = active_store
    result = run()
    assert result.status == "has_supported_candidates"
    assert lookup.call_count == 2 and active.call_count == 1 and loader.call_count == 1
    lookup.assert_any_call("release-1", "nutritional_ingredients_eu", candidate_id="breast-a")


def test_a_run_change_after_inference_removes_all_candidate_ranks(active_store, model):
    run, loader, lookup, active = active_store
    active.return_value = {"run_id": "new-run"}
    result = run()
    assert result.status == "blocked" and result.reasons == ["active_profile_changed"]
    assert all(item.rank is None and item.status == "needs_evidence" for item in result.candidates)


def test_stale_run_is_rejected_before_source_reads_or_inference(active_store, model):
    run, loader, lookup, active = active_store
    assert run(baseline_run_id="old-run").status == "blocked"
    lookup.assert_not_called()
    assert model[0] == []


def test_missing_candidate_records_are_reported_without_name_fallback(active_store, model):
    run, loader, lookup, active = active_store
    lookup.side_effect = lambda *args, **kwargs: []
    result = run()
    assert result.status == "needs_evidence"
    assert all(item.reasons == ["candidate_composition_not_found"] for item in result.candidates)
    assert model[0] == []


def test_database_outages_are_not_presented_as_missing_food_evidence(active_store, model):
    run, loader, lookup, active = active_store
    lookup.side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError):
        run()


def test_a_different_loaded_record_cannot_replace_the_requested_composition(inputs, active_store, model):
    run, loader, lookup, active = active_store
    lookup.side_effect = lambda *args, **kwargs: [{"normalized": inputs[1][1]}]
    result = run()
    wrong = next(item for item in result.candidates if item.composition.candidate_id == "breast-a")
    assert wrong.status == "rejected" and wrong.reasons == ["candidate_reference_mismatch"]
    assert wrong.rank is None


def test_one_uncertain_culinary_result_does_not_falsely_reject_or_approve_other_candidates(inputs, model):
    model[1]["breast-b"] = "uncertain"
    result = run_preview(inputs)
    assert result.candidates[0].status == "ready_for_instruction_editing" and result.candidates[0].rank == 1
    assert result.candidates[1].status == "needs_evidence" and result.candidates[1].rank is None


def test_unestablished_roles_never_allow_candidate_readiness(inputs, model, monkeypatch):
    request = culinary._request
    def uncertain(payload, response_model, *args, **kwargs):
        if response_model is RoleAssessment:
            return json.dumps({"status": "uncertain", "roles": [], "uncertainties": ["Function is unclear."]})
        return request(payload, response_model, *args, **kwargs)
    monkeypatch.setattr(culinary, "_request", uncertain)
    result = run_preview(inputs)
    assert result.roles.status == "uncertain" and result.status == "needs_evidence"
    assert all(item.rank is None for item in result.candidates)


def test_an_invalid_mutated_culinary_wrapper_cannot_claim_numerical_approval(inputs, model, monkeypatch):
    original = service.assess_contextual_candidates
    def invalid(*args):
        result = original(*args)
        result.numerical_approval = True
        return result
    monkeypatch.setattr(service, "assess_contextual_candidates", invalid)
    result = run_preview(inputs)
    assert result.status == "needs_evidence" and result.culinary_batch is None
    assert all(item.rank is None for item in result.candidates)


def test_equal_improvements_have_stable_ranks_independent_of_input_order(inputs, model):
    inputs[1][0].values_per_100g["energy_kcal"] = 100
    first = run_preview(inputs)
    second = run_preview((inputs[0], list(reversed(inputs[1]))))
    assert {item.candidate_id: item.rank for item in first.candidates} == {item.candidate_id: item.rank for item in second.candidates}


def test_empty_or_unresolved_source_pools_do_not_trigger_model_calls(inputs, model):
    empty = run_preview((inputs[0], []))
    assert empty.status == "no_supported_candidates" and not empty.candidates
    inputs[0].ingredients[0].composition = None
    unresolved = run_preview(inputs)
    assert unresolved.status == "needs_evidence" and all(item.reasons == ["source_composition"] for item in unresolved.candidates)
    assert model[0] == []


def test_bad_source_data_is_rejected_without_overwriting_other_candidate_outcomes(active_store, model, monkeypatch):
    run, loader, lookup, active = active_store
    def decode(row):
        if row["normalized"].ref.candidate_id == "breast-a":
            raise ComparisonLoadError("released_nutrient_values_mismatch", blocked=True)
        return row["normalized"]
    monkeypatch.setattr(service, "_composition", decode)
    result = run()
    assert result.status == "has_supported_candidates"
    rejected = next(item for item in result.candidates if item.composition.candidate_id == "breast-a")
    assert rejected.status == "rejected" and rejected.rank is None
    assert rejected.reasons == ["released_nutrient_values_mismatch"]
