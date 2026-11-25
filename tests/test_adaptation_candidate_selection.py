"""Compatibility and completeness are screened before the candidate limit."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from test_adaptation_preview import inputs, model, composition

from recipe_wrangler.repositories import matching_store
from recipe_wrangler.services.adaptation import candidate_selection as selection
from recipe_wrangler.services.adaptation import preview as service
from recipe_wrangler.services.adaptation.models import ComparisonConstraints
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.services.adaptation.production_candidates import make_substitution_edit
from recipe_wrangler.services.adaptation.comparison import compare_raw_part


@pytest.fixture
def catalogue(inputs, monkeypatch):
    def install(candidates):
        from recipe_wrangler.services.adaptation import service
        monkeypatch.setattr(service, "_select_graph_name", lambda *args: None)
        records = [{**item.ref.model_dump(), "candidate": {"document": item.ref.candidate_id}} for item in candidates]
        by_id = {item.ref.candidate_id: item for item in candidates}
        query = Mock(return_value=records)
        monkeypatch.setattr(matching_store, "food_part_composition_records", query)
        monkeypatch.setattr(selection, "_composition", lambda row: by_id[row["candidate_id"]].model_copy(deep=True))
        return query, records
    return install


def shortlist(inputs, **kwargs):
    return selection.shortlist_raw_part_candidates(inputs[0], target_position=0,
        objective=kwargs.pop("objective", NutrientObjective(metric="energy_kcal")), **kwargs)


def test_invalid_alphabetical_results_cannot_hide_later_eligible_candidates(inputs, catalogue):
    invalid = [composition(f"a-invalid-{i}", "breast", 100) for i in range(20)]
    for item in invalid: item.properties["qualifiers.coating"].value = "breaded"
    query, records = catalogue(invalid + inputs[1])
    before = deepcopy(inputs[0].model_dump())
    result = shortlist(inputs, limit=1)
    assert len(result.screening) == 22
    assert [item.candidate_id for item in result.candidate_refs] == ["breast-b"]
    assert sum(item.selected for item in result.screening) == 1
    assert all(item.status == "rejected" for item in result.screening[:20])
    query.assert_called_once_with("release-1", "nutritional_ingredients_irish", dataset="synthetic-table",
        source_release="1", identity="chicken", biological_source="chicken", source_food_part="thigh")
    assert inputs[0].model_dump() == before


def test_original_record_and_repeated_candidates_are_removed(inputs, catalogue, monkeypatch):
    _, records = catalogue(inputs[1])
    original = inputs[0].ingredients[0].composition.ref.model_dump()
    monkeypatch.setattr(selection, 'food_part_candidate_records', lambda *args:
                        ([original, records[0], records[0]], []))
    result = shortlist(inputs)
    assert len(result.screening) == 1
    assert 'unchanged_source_composition_removed' in result.reasons


@pytest.mark.parametrize("issue", ["state", "skin", "bone", "source", "region", "release", "no_benefit"])
def test_conflicting_candidates_are_excluded_before_selection(inputs, catalogue, issue):
    bad = inputs[1][0]
    if issue in {"state", "skin", "bone"}:
        key, value = {"state": ("preparation_state", "cooked"), "skin": ("skin_status", "skin_on"),
                      "bone": ("bone_status", "bone_in")}[issue]
        bad.properties[key].value = value
    elif issue == "source": bad.source_dataset = "other"
    elif issue == "region": bad.ref.collection = "nutritional_ingredients_hungarian"
    elif issue == "release": bad.ref.release_id = "other"
    else: bad.values_per_100g["energy_kcal"] = 220
    catalogue(inputs[1])
    result = shortlist(inputs)
    assert [item.candidate_id for item in result.candidate_refs] == ["breast-b"]
    assert result.screening[0].status == "rejected"


@pytest.mark.parametrize("issue", ["quantity_basis", "missing_energy", "unknown_property", "missing_serves", "allergen"])
def test_unknown_evidence_never_fills_the_shortlist(inputs, catalogue, issue):
    kwargs = {}
    if issue == "quantity_basis": inputs[0].ingredients[0].quantity.basis = "unknown"
    elif issue == "missing_energy":
        for item in inputs[1]: item.values_per_100g["energy_kcal"] = None
    elif issue == "unknown_property":
        for item in inputs[1]: item.properties.pop("skin_status")
    elif issue == "missing_serves": inputs[0].serves = None
    else: kwargs["constraints"] = ComparisonConstraints(forbidden_allergens={"milk"})
    catalogue(inputs[1])
    result = shortlist(inputs, **kwargs)
    assert result.status == "needs_evidence" and not result.candidate_refs
    assert all(item.status == "needs_evidence" and not item.selected for item in result.screening)


def test_increase_goal_and_stable_ties(inputs, catalogue):
    inputs[1][0].values_per_100g["protein_g"] = 24
    catalogue(list(reversed(inputs[1])))
    result = shortlist(inputs, objective=NutrientObjective(metric="protein_g", direction="increase"), limit=1)
    assert result.candidate_refs[0].candidate_id == "breast-a"


def test_no_source_does_not_query_catalogue(inputs, catalogue):
    query, records = catalogue(inputs[1])
    inputs[0].ingredients[0].composition = None
    assert shortlist(inputs).reasons == ["source_composition"]
    query.assert_not_called()


def test_edit_intent_uses_observed_cut_without_lending_it_to_source_composition(inputs):
    snapshot, targets = inputs
    snapshot.input_policy = "production_estimate"
    item = snapshot.ingredients[0]
    item.composition.properties.pop("food_part")
    item.composition.properties.pop("biological_source")
    before = snapshot.model_dump()
    edit = make_substitution_edit(snapshot, 0, targets[0])
    assert edit.source_food_part == "thigh" and edit.target_food_part == "breast"
    assert edit.source_biological_source == edit.target_biological_source == "chicken"
    result = compare_raw_part(snapshot, edit, targets[0])
    assert "variant_identity_change_requires_ingredient_operation" not in result.blockers
    assert "source.property:food_part" in result.missing_evidence
    assert "source.property:biological_source" in result.missing_evidence
    assert result.status == "needs_evidence"
    assert snapshot.model_dump() == before


def test_outages_propagate(inputs, catalogue):
    query, records = catalogue(inputs[1])
    query.side_effect = OperationalError("SELECT", {}, Exception("offline"))
    with pytest.raises(OperationalError): shortlist(inputs)


@pytest.mark.parametrize("limit", [0, 11, True, 1.5])
def test_limit_is_bounded_before_reads(inputs, catalogue, limit):
    query, records = catalogue(inputs[1])
    with pytest.raises(ValueError): shortlist(inputs, limit=limit)
    query.assert_not_called()


@pytest.mark.parametrize("stale", [False, True])
def test_active_preview_automatically_selects_then_assesses(inputs, catalogue, model, monkeypatch, stale):
    query, records = catalogue(inputs[1])
    by_id = {item.ref.candidate_id: item for item in inputs[1]}
    monkeypatch.setattr(service, "load_active_snapshot", lambda *args: inputs[0].model_copy(deep=True))
    monkeypatch.setattr(service, "_composition", lambda row: by_id[row["candidate_id"]])
    monkeypatch.setattr(matching_store, "composition_records", lambda release, collection, *, candidate_id: [
        row for row in records if row["candidate_id"] == candidate_id])
    monkeypatch.setattr(matching_store, "active_profile", lambda *args, **kwargs: {"run_id": "changed" if stale else inputs[0].run_id})
    result = service.preview_active_raw_part_candidates("recipe-1", "irish", baseline_run_id="run-1",
        quantity_input_hash="quantity-1", title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        target_position=0, objective=NutrientObjective(metric="energy_kcal"))
    assert result.candidate_selection.status == "selected"
    if stale:
        assert result.status == "blocked" and all(item.rank is None for item in result.candidates)
        return
    assert result.status == "has_supported_candidates" and len(model[0]) == 2
    assert next(item for item in result.candidates if item.rank == 1).composition.candidate_id == "breast-b"


def test_active_preview_retains_missing_evidence_and_never_calls_model(inputs, catalogue, monkeypatch):
    catalogue(inputs[1])
    inputs[0].ingredients[0].quantity.basis = "unknown"
    monkeypatch.setattr(service, "load_active_snapshot", lambda *args: inputs[0])
    monkeypatch.setattr(matching_store, "active_profile", lambda *args, **kwargs: {"run_id": "run-1"})
    monkeypatch.setattr(service, "infer_contextual_roles", lambda *args: pytest.fail("Unexpected model call"))
    result = service.preview_active_raw_part_candidates("recipe-1", "irish", baseline_run_id="run-1",
        quantity_input_hash="quantity-1", title="Chicken dish", instructions=["Cook the chicken thigh gently."],
        target_position=0, objective=NutrientObjective(metric="energy_kcal"))
    assert result.status == "needs_evidence" and not result.candidates
    assert len(result.candidate_selection.screening) == 2


def test_graph_fallback_runs_after_all_part_candidates_fail(inputs, catalogue, monkeypatch):
    _, records = catalogue(inputs[1])
    inputs[1][0].values_per_100g['energy_kcal'] = 300
    monkeypatch.setattr(selection, 'food_part_candidate_records', lambda *a: ([records[0]], []))
    graph = Mock(return_value=([records[1]], []))
    monkeypatch.setattr(selection, 'production_candidate_records', graph)
    result = shortlist(inputs, allow_general_substitutions=True)
    graph.assert_called_once()
    assert [ref.candidate_id for ref in result.candidate_refs] == ['breast-b']


def test_eligible_part_candidate_avoids_graph_fallback(inputs, catalogue, monkeypatch):
    catalogue(inputs[1])
    graph = Mock(side_effect=AssertionError('unnecessary fallback'))
    monkeypatch.setattr(selection, 'production_candidate_records', graph)
    assert shortlist(inputs, allow_general_substitutions=True).status == 'selected'
    graph.assert_not_called()


def test_nonraw_source_reaches_graph_when_facet_search_is_empty(inputs, monkeypatch):
    inputs[0].input_policy = 'production_estimate'
    inputs[0].ingredients[0].composition.properties['preparation_state'].value = 'cooked'
    monkeypatch.setattr(selection, 'nutritional_variant_records', lambda *a: [])
    graph = Mock(return_value=([], []))
    monkeypatch.setattr(selection, 'production_candidate_records', graph)
    result = shortlist(inputs, allow_general_substitutions=True,
                       objective=NutrientObjective(metric='saturated_fat_g'))
    graph.assert_called_once()
    assert not result.candidate_refs
