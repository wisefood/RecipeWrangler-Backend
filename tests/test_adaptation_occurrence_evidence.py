"""Quoted occurrence evidence stays separate from observations and mass basis."""

from copy import deepcopy

import pytest

from test_adaptation_comparison_store import database
from test_adaptation_preview import inputs

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.services.adaptation import comparison_store as adapter
from recipe_wrangler.services.adaptation import occurrence_evidence as recovery
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import RawPartEdit
from recipe_wrangler.services.adaptation.contextual_assessment import build_adaptation_context


@pytest.fixture
def source_case(database, monkeypatch):
    terms = [{'term_id': name, 'preferred_label': name, 'definition': '', 'curation_status': '',
              'labels': [{'text': name, 'type': 'preferred'}]} for name in ("chicken", "pork", "tuna", "shrimp food product")]
    annotate = HybridAnnotator(ContextualParser(IngredientDictionary({'schema_version': 1, 'terms': terms}),
        {"chicken": {"meat"}, "pork": {"meat"}, "tuna": {"generic"}})).annotate
    monkeypatch.setattr(recovery, "_annotator", lambda: annotate)
    monkeypatch.setattr(adapter, "recover_source_evidence", recovery.recover_source_evidence)
    active, source, target, edit, reads, lookup = database
    detail = active["profile"]["details"][0]
    detail.update(ingredient="chicken thigh", name="chicken thigh", original_ingredient_text="",
                  observed_facts=annotate("chicken thigh"))
    detail["quantity_evidence"]["source_line"] = "200g skinless boneless chicken thigh"
    return database, detail


def load(case):
    return adapter.load_active_snapshot("recipe-1", "irish")


def test_unknown_saved_identity_is_recovered_from_quoted_dictionary_alias(source_case):
    _, detail = source_case
    detail.update(ingredient='prawns', name='prawns', weight_g=500,
        original_ingredient_text='500g peeled raw prawns, deveined, tails intact',
        observed_facts={'facets': {'base_identity': None}}, quantity_evidence=None)
    before = deepcopy(detail)
    overlay = recovery.recover_source_evidence(detail, recipe_id='recipe-1', run_id='run-1', quantity_hash='hash')
    assert overlay.properties['base_identity'].value == 'shrimp'
    assert overlay.properties['preparation_state'].value == 'raw'
    support = overlay.property_support['base_identity'][0]
    span = support['spans'][0]
    assert overlay.source_line[span['start']:span['end']] == support['quote'] == 'prawns'
    assert detail == before


@pytest.mark.parametrize('line', ['500g prawns and garlic', '500g tuna', '500g prawns, prawns'])
def test_identity_overlay_does_not_guess_compound_mismatched_or_repeated_mentions(source_case, line):
    _, detail = source_case
    detail.update(ingredient='prawns', name='prawns', weight_g=500,
        original_ingredient_text=line, observed_facts={'facets': {'base_identity': None}}, quantity_evidence=None)
    overlay = recovery.recover_source_evidence(detail, recipe_id='recipe-1', run_id='run-1', quantity_hash='hash')
    assert 'base_identity' not in overlay.properties


def test_recovers_skin_bone_quotes_but_never_raw_edible_mass_or_saved_facts(source_case):
    database, detail = source_case
    before = deepcopy(database[0])
    snapshot = load(source_case)
    item = snapshot.ingredients[0]
    overlay = item.source_evidence
    assert overlay.properties["skin_status"].value == "skinless"
    assert overlay.properties["bone_status"].value == "boneless"
    assert overlay.properties["skin_status"].evidence.status == "stated"
    assert overlay.property_support["skin_status"][0]["quote"] == "skinless"
    assert item.observed_properties.get("skin_status") is None
    assert item.original_text == "" and item.quantity.basis == "unknown"
    assert database[0] == before
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert result.source_evidence == overlay
    assert "observed.property:skin_status" not in result.missing_evidence
    assert "observed.property:bone_status" not in result.missing_evidence
    assert "observed.property:preparation_state" in result.missing_evidence
    assert "occurrence.raw_edible_mass" in result.missing_evidence
    assert result.observed_properties == item.observed_properties


def test_explicit_raw_property_still_does_not_declare_edible_quantity_basis(source_case):
    database, detail = source_case
    detail["quantity_evidence"]["source_line"] = "200g raw skinless boneless chicken thigh"
    snapshot = load(source_case)
    assert snapshot.ingredients[0].source_evidence.properties["preparation_state"].value == "raw"
    assert snapshot.ingredients[0].quantity.basis == "unknown"


def test_full_original_line_supplies_state_even_when_quantity_is_estimated(source_case):
    database, detail = source_case
    detail["quantity_evidence"] = None
    detail["original_ingredient_text"] = "cooked boneless skinless chicken thigh"
    snapshot = adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=True)
    assert snapshot.ingredients[0].quantity.evidence.status == "proxy"
    overlay = snapshot.ingredients[0].source_evidence
    assert overlay.properties["preparation_state"].value == "cooked"
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert result.status == "blocked"
    assert "observed.property_conflict:preparation_state" in result.blockers


def test_conflicting_saved_assertions_block_instead_of_being_overwritten(source_case):
    database, detail = source_case
    detail["observed_facts"]["facets"]["skin_status"] = "present"
    detail["observed_facts"]["evidence"].append({"field": "facets.skin_status", "quote": "chicken", "polarity": "asserted"})
    snapshot = load(source_case)
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert result.status == "blocked" and "source_evidence_conflict:skin_status" in result.blockers
    assert result.observed_properties["skin_status"].value == "present"


def test_inconsistent_source_mass_cannot_supply_properties(source_case):
    database, detail = source_case
    detail["quantity_evidence"]["source_line"] = "300g skinless boneless chicken thigh"
    snapshot = load(source_case)
    assert snapshot.ingredients[0].source_evidence.deferred_reason == "source_quantity_evidence_invalid"
    assert not snapshot.ingredients[0].source_evidence.properties
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert "source_quantity_evidence_invalid" in result.blockers


@pytest.mark.parametrize("line", ["200g pork thigh", "200g chicken thigh or chicken breast"])
def test_other_foods_and_ambiguous_source_lines_do_not_create_claims(source_case, line):
    source_case[1]["quantity_evidence"]["source_line"] = line
    overlay = load(source_case).ingredients[0].source_evidence
    assert overlay.deferred_reason and not overlay.properties


def test_recovered_dicing_is_preserved_and_cannot_be_ignored_to_pass(source_case):
    database, detail = source_case
    detail["quantity_evidence"]["source_line"] = "200g raw skinless boneless chicken thigh, diced"
    snapshot = load(source_case)
    assert snapshot.ingredients[0].source_evidence.properties["processes"].value == ["diced"]
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert result.status == "needs_evidence"
    assert "source.property:processes" in result.missing_evidence
    assert "target.property:processes" in result.missing_evidence
    assert result.source_evidence.properties["processes"].value == ["diced"]


@pytest.mark.parametrize("field,value", [("recipe_id", "other"), ("baseline_run_id", "other"),
    ("quantity_input_hash", "other"), ("position", 1), ("weight_g", 300), ("source_line", "changed")])
def test_stale_or_mutated_overlay_never_approves(source_case, field, value):
    database, detail = source_case
    snapshot = load(source_case)
    setattr(snapshot.ingredients[0].source_evidence, field, value)
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert "source_evidence_scope_mismatch" in result.blockers


def test_source_text_is_visible_in_context_without_overwriting_snapshot(source_case):
    snapshot = load(source_case)
    before = snapshot.model_dump()
    context = build_adaptation_context(snapshot, title="Chicken dish", instructions=["Cook the chicken thigh."],
        objective="Reduce energy", target_position=0)
    assert context.ingredients[0].original_text == source_case[1]["quantity_evidence"]["source_line"]
    assert context.ingredients[0].source_evidence == snapshot.ingredients[0].source_evidence
    assert snapshot.model_dump() == before


def test_source_whitespace_and_quote_spans_are_exact(source_case):
    source_case[1]["quantity_evidence"]["source_line"] = "  200g skinless boneless chicken thigh  "
    overlay = load(source_case).ingredients[0].source_evidence
    assert overlay.source_line.startswith("  ")
    for support in overlay.property_support.values():
        for evidence in support:
            for span in evidence["spans"]:
                assert overlay.source_line[span["start"]:span["end"]] == evidence["quote"]


def test_missing_source_line_does_not_invoke_parser(source_case, monkeypatch):
    source_case[1]["quantity_evidence"].pop("source_line")
    monkeypatch.setattr(recovery, "_annotator", lambda: pytest.fail("Unexpected annotation"))
    assert load(source_case).ingredients[0].source_evidence is None


def test_missing_assets_defer_recovery_without_creating_claims(source_case, monkeypatch):
    def unavailable():
        raise RuntimeError("Missing release asset")
    monkeypatch.setattr(recovery, "_annotator", unavailable)
    overlay = load(source_case).ingredients[0].source_evidence
    assert not overlay.properties and overlay.deferred_reason == "source_annotation_assets_unavailable"


def test_recovered_claims_support_an_otherwise_complete_comparison(source_case, inputs):
    snapshot, targets = inputs
    item = snapshot.ingredients[0]
    item.observed_properties.pop("skin_status")
    item.observed_properties.pop("bone_status")
    item.source_evidence = recovery.recover_source_evidence({"ingredient": "chicken thigh", "position": 0,
        "weight_g": 200, "quantity_evidence": {"kind": "explicit_source_mass", "weight_g": 200,
        "source_line": "200g skinless boneless chicken thigh"}}, recipe_id=snapshot.recipe_id,
        run_id=snapshot.run_id, quantity_hash=snapshot.quantity_input_hash)
    edit = RawPartEdit(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        source_identity="chicken", target_identity="chicken", source_biological_source="chicken",
        target_biological_source="chicken", source_food_part="thigh", target_food_part="breast",
        quantity_input_hash=snapshot.quantity_input_hash, position=0,
        from_composition=item.composition.ref, to_composition=targets[0].ref)
    result = compare_raw_part(snapshot, edit, targets[0])
    assert result.status == "supported_input_comparison"
    assert result.recipe_delta_per_serving["energy_kcal"] == -60
    assert "skin_status" not in result.observed_properties
    assert result.source_evidence.properties["skin_status"].value == "skinless"


def test_first_source_branch_recovers_own_removal_claims_and_full_line_spans(source_case):
    database, detail = source_case
    detail["quantity_evidence"] = None
    detail["original_ingredient_text"] = "  8 chicken thighs, skin and bone removed (or 4 chicken legs, with skin)"
    before = deepcopy(database[0])
    overlay = adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=True).ingredients[0].source_evidence
    assert overlay.selected_source_text == "8 chicken thighs, skin and bone removed"
    assert overlay.source_line == detail["original_ingredient_text"]
    assert overlay.deferred_reason is None
    assert overlay.properties["skin_status"].value == "skinless"
    assert overlay.properties["bone_status"].value == "boneless"
    for supports in overlay.property_support.values():
        for support in supports:
            for span in support["spans"]:
                assert overlay.source_line[span["start"]:span["end"]] == support["quote"]
                assert span["end"] <= overlay.source_line.index(" (or")
    assert database[0] == before


@pytest.mark.parametrize("line", [
    "8 chicken thighs (or 4 skinless chicken legs)",
    "8 chicken thighs, skin to be removed (or 4 chicken legs)",
    "8 chicken thighs, to have skin removed (or 4 chicken legs)",
    "8 chicken thighs, not skin removed (or 4 chicken legs)",
])
def test_unselected_or_future_removal_never_establishes_skinless(source_case, line):
    _, detail = source_case
    detail["quantity_evidence"] = None
    detail["original_ingredient_text"] = line
    overlay = adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=True).ingredients[0].source_evidence
    assert "skin_status" not in overlay.properties


@pytest.mark.parametrize("name,line", [
    ("chicken thigh", "4 chicken breasts (or 8 chicken thighs)"),
    ("chicken thigh", "8 chicken thighs (skin removed or bone removed)"),
    ("chicken thigh", "8 chicken thighs (or 4 chicken legs), cooked"),
])
def test_branch_scope_requires_first_food_and_no_unresolved_shared_tail(name, line):
    assert recovery.selected_source_branch(name, line) is None


def test_tampered_selected_branch_blocks_comparison(source_case):
    database, detail = source_case
    detail["quantity_evidence"] = None
    detail["original_ingredient_text"] = "8 chicken thighs, skin removed (or 4 chicken legs)"
    snapshot = adapter.load_active_snapshot("recipe-1", "irish", allow_input_estimates=True)
    snapshot.ingredients[0].source_evidence.selected_source_text = "4 chicken legs"
    result = compare_raw_part(snapshot, database[3], adapter._composition(database[2]))
    assert "source_alternative_selection_mismatch" in result.blockers
