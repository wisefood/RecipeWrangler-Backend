"""Production-shaped records must retain restriction evidence without inventing absence."""

import pytest

from test_adaptation_comparison_store import record
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import ComparisonConstraints, ComparisonSnapshot
from recipe_wrangler.services.adaptation.comparison_store import _composition
from recipe_wrangler.services.adaptation.production_candidates import make_substitution_edit
from recipe_wrangler.services.adaptation.comparison import restriction_text_concerns


def released(identity, part):
    row = record(part)
    label = "raw " + identity
    row["annotation"]["label"] = row["candidate"]["document"] = label
    for key in ("source_record", "source_profile"):
        row[key]["label"] = label
    row["annotation"]["text_annotation"] = {"input_text": label, "parsed_text": label,
        "facets": {"base_identity": identity, "preparation_state": "raw"}, "qualifiers": {},
        "modifiers": [], "evidence": [
            {"field": "main_ingredient", "quote": identity, "polarity": "asserted"},
            {"field": "facets.preparation_state", "quote": "raw", "polarity": "asserted"}]}
    return _composition(row)


def baseline(source):
    return ComparisonSnapshot(recipe_id="recipe-1", run_id="run-1", nutrition_source="irish",
        release_id="release-1", quantity_input_hash="quantity-1", serves=2., input_policy="production_estimate",
        ingredients=[{"position": 0, "name": source.provenance["candidate"]["document"],
            "original_text": source.provenance["candidate"]["document"], "composition": source,
            "observed_properties": source.properties,
            "quantity": {"weight_g": 200., "basis": "selected_state_edible", "evidence": {
                "reference": "saved-run", "method": "saved_production_input_estimate", "version": "1", "status": "proxy"}}}])


def test_loaded_named_plant_properties_enable_a_diet_restricted_comparison():
    source, target = released("romaine lettuce", "thigh"), released("spinach", "breast")
    snapshot = baseline(source)
    edit = make_substitution_edit(snapshot, 0, target)
    result = compare_raw_part(snapshot, edit, target, ComparisonConstraints(required_consumer_groups={"vegan", "vegetarian"}))
    assert result.status == "supported_input_comparison", result.missing_evidence + result.blockers
    assertion = result.target_composition.consumer_assessments["vegan"]
    assert assertion.value == "suitable" and assertion.evidence.status == "stated"
    assert "openfoodfacts" in assertion.evidence.reference
    assert len(assertion.evidence.version) == 64


@pytest.mark.parametrize("identity", ["unknown sauce", "romaine lettuce with milk", "romaine lettuce (may contain milk)"])
def test_compounds_and_unknowns_do_not_borrow_plain_plant_suitability(identity):
    source, target = released("romaine lettuce", "thigh"), released(identity, "breast")
    snapshot = baseline(source)
    result = compare_raw_part(snapshot, make_substitution_edit(snapshot, 0, target), target,
        ComparisonConstraints(required_consumer_groups={"vegan"}))
    assert result.status != "supported_input_comparison"


def test_named_plant_diet_evidence_does_not_certify_allergen_absence():
    source, target = released("romaine lettuce", "thigh"), released("spinach", "breast")
    snapshot = baseline(source)
    result = compare_raw_part(snapshot, make_substitution_edit(snapshot, 0, target), target,
        ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result.status == "needs_evidence"
    assert "milk" not in target.allergen_assessments


def test_compound_allergen_detection_is_retained_as_inference():
    target = released("romaine lettuce with milk", "breast")
    assert target.allergen_assessments["milk"].value == "present"
    assert target.allergen_assessments["milk"].evidence.status == "inferred"


def test_selected_plant_composition_does_not_hide_unchanged_original_milk():
    source, target = released("romaine lettuce", "thigh"), released("spinach", "breast")
    snapshot = baseline(source)
    extra = snapshot.ingredients[0].model_copy(deep=True)
    extra.position, extra.original_text = 1, "romaine lettuce with milk"
    snapshot.ingredients.append(extra)
    result = compare_raw_part(snapshot, make_substitution_edit(snapshot, 0, target), target,
        ComparisonConstraints(required_consumer_groups={"vegan"}))
    assert result.status == "needs_evidence"
    assert "occurrence:1.source_text_restriction:consumer:vegan" in result.missing_evidence


@pytest.mark.parametrize("text,concerns", [
    ("Serve with cream.", {"consumer:vegan", "allergen:milk"}),
    ("Stir in peanut butter.", {"allergen:peanut"}),
    ("Chop the romaine lettuce.", set()),
])
def test_instruction_only_ingredients_cannot_bypass_restrictions(text, concerns):
    result = restriction_text_concerns(text, ComparisonConstraints(
        required_consumer_groups={"vegan"}, forbidden_allergens={"milk", "peanut"}))
    assert set(result) == concerns
