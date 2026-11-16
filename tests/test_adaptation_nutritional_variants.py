"""Existing facet extraction drives same-product substitutions; no live models."""

from copy import deepcopy
import json

import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.services.adaptation import candidate_selection, contextual_assessment, instruction_editor
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import ComparisonComposition, ComparisonIngredient, ComparisonQuantity, ComparisonSnapshot, ComparisonConstraints
from recipe_wrangler.services.adaptation.comparison_store import _assertions
from recipe_wrangler.services.adaptation.contextual_assessment import build_adaptation_context
from recipe_wrangler.services.adaptation.enhancement_pipeline import enhance_recipe
from recipe_wrangler.services.adaptation.instruction_editor import _allowed_patches
from recipe_wrangler.services.adaptation.nutritional_variants import describe_change, product_signature, recover_product_claims
from recipe_wrangler.services.adaptation.objective_approval import InputScoreContext
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.services.adaptation.production_candidates import make_substitution_edit
from recipe_wrangler.tools.nutrition_completeness import METRICS
from test_adaptation_enhancement import breakdown


@pytest.fixture
def products():
    names = ("soy sauce", "milk", "yogurt", "butter")
    terms = [{"term_id": name, "preferred_label": name, "definition": "", "curation_status": "",
        "labels": [{"text": name, "type": "preferred"}]} for name in names]
    annotate = HybridAnnotator(ContextualParser(IngredientDictionary({"schema_version": 1, "terms": terms}),
        {"soy sauce": {"sauce"}, "milk": {"milk"}, "yogurt": {"milk"}})).annotate
    def make(label, identifier, metric, value):
        annotation = annotate(label)
        evidence = {"reference": "synthetic-product-fixture", "method": "fixture", "version": "1", "status": "stated"}
        values = dict.fromkeys(METRICS, 0.0)
        values.update(energy_kcal=100., protein_g=3.)
        values[metric] = value
        return ComparisonComposition.model_validate({"ref": {"release_id": "fixture-release", "collection": "nutritional_ingredients_eu", "candidate_id": identifier},
            "source_dataset": "synthetic", "source_release": "1", "basis": {"amount_g": 100., "portion": "edible", "evidence": evidence},
            "properties": recover_product_claims(_assertions(annotation, "fixture"), label, 'fixture'), "values_per_100g": values,
            "nutrient_definitions": {metric: "synthetic:" + metric for metric in METRICS},
            "provenance": {"annotation": {"text_annotation": annotation}, "candidate": {"document": label}}})
    return make


def snapshot(source):
    evidence = {"reference": "saved-input-fixture", "method": "saved_production_input_estimate", "version": "1", "status": "proxy"}
    return ComparisonSnapshot(recipe_id="fixture-recipe", run_id="fixture-run", nutrition_source="irish",
        release_id="fixture-release", quantity_input_hash="fixture-quantities", serves=2., input_policy="production_estimate",
        ingredients=[ComparisonIngredient(position=0, name=source.provenance["candidate"]["document"].lower(),
            original_text="100 g " + source.provenance["candidate"]["document"].lower(),
            quantity=ComparisonQuantity(weight_g=100., basis="selected_state_edible", evidence=evidence),
            observed_properties=deepcopy(source.properties), composition=source)])


@pytest.mark.parametrize("original,replacement,metric,facet", [
    ("Soy sauce", "Soy sauce, reduced sodium", "sodium_mg", "sodium_level"),
    ("Milk, whole", "Milk, skimmed", "saturated_fat_g", "fat_level"),
    ("Yogurt", "Yogurt, sugar-free", "sugar_g", "sugar_level"),
    ("Milk, whole", "Milk, low-fat", "energy_kcal", "fat_level"),
])
def test_existing_facets_authorize_an_explicit_same_product_change(products, original, replacement, metric, facet):
    source, target = products(original, "source", metric, 1000.), products(replacement, "target", metric, 100.)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric=metric)
    assert edit.nutritional_change.facet == facet
    assert edit.source_identity == edit.target_identity
    assert product_signature(source, facet) == product_signature(target, facet)
    result = compare_raw_part(baseline, edit, target)
    assert result.status == "supported_input_comparison", result.blockers + result.missing_evidence
    assert result.ingredient_delta[metric] == -900.
    assert source.properties.get("preparation_state") is None


def test_unspecified_original_claim_remains_unknown(products):
    source, target = products("Soy sauce", "source", "sodium_mg", 1000.), products("Soy sauce, low sodium", "target", "sodium_mg", 100.)
    edit = make_substitution_edit(snapshot(source), 0, target, nutritional_metric="sodium_mg")
    assert edit.nutritional_change.from_value is None
    assert "sodium_level" not in source.properties


def test_plain_whole_milk_yogurt_can_use_plain_low_fat_yogurt(products):
    source = products('Yogurt, whole milk, plain', 'source', 'saturated_fat_g', 3.)
    target = products('Yogurt, low fat, plain', 'target', 'saturated_fat_g', 1.)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric='saturated_fat_g')
    result = compare_raw_part(baseline, edit, target)
    assert result.status == 'supported_input_comparison', result.blockers + result.missing_evidence


def test_explicit_light_butter_label_supports_replacement_and_removes_old_fat_percentage(products):
    source = products('Butter, 80% fat minimum, unsalted', 'source', 'saturated_fat_g', 60.)
    target = products('Butter, light, 39-41% fat, unsalted', 'target', 'saturated_fat_g', 26.2)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric='saturated_fat_g')
    assert edit.nutritional_change.to_value == 'reduced'
    comparison = compare_raw_part(baseline, edit, target)
    assert comparison.status == 'supported_input_comparison', comparison.blockers + comparison.missing_evidence
    context = build_adaptation_context(baseline, title='Mushrooms', instructions=['Sauté in butter.'],
        objective='Reduce saturated fat', target_position=0)
    patches, texts = _allowed_patches(context, edit)
    for patch in sorted(patches, key=lambda p: p.start, reverse=True):
        key = patch.kind, patch.reference_id
        texts[key] = texts[key][:patch.start] + patch.after + texts[key][patch.end:]
    assert texts['ingredient_text', 'ingredient:0'] == '100 g reduced-fat butter, unsalted'


def test_light_without_supported_butter_fat_evidence_does_not_establish_a_claim(products):
    target = products('Butter, light', 'target', 'saturated_fat_g', 1.)
    target.properties.pop('fat_level', None)
    assert recover_product_claims(target.properties, 'Butter, light', 'fixture').get('fat_level') is None


@pytest.mark.parametrize('label', ['Yogurt, low fat, hazelnut', 'Yogurt, low fat, fruit', 'Yogurt powder, low fat, plain'])
def test_plain_yogurt_does_not_silently_become_flavoured_or_powdered(products, label):
    source = products('Yogurt, whole milk, plain', 'source', 'saturated_fat_g', 3.)
    target = products(label, 'target', 'saturated_fat_g', 1.)
    edit = make_substitution_edit(snapshot(source), 0, target, nutritional_metric='saturated_fat_g')
    assert 'nutritional_variant_product_description_mismatch' in compare_raw_part(snapshot(source), edit, target).blockers


@pytest.mark.parametrize("issue", ["worse_values", "powder", "state", "claim_inferred", "unexpected_sugar", "tampered_change"])
def test_claims_cannot_override_numeric_product_state_or_source_checks(products, issue):
    source = products("Milk, whole", "source", "saturated_fat_g", 10.)
    label = "Milk powder, low-fat" if issue == "powder" else "Milk, low-fat"
    target = products(label, "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    if issue == "worse_values": target.values_per_100g["saturated_fat_g"] = 20.
    elif issue == "state":
        from recipe_wrangler.services.adaptation.models import PropertyAssertion
        target.properties["preparation_state"] = PropertyAssertion(value="cooked", evidence=target.basis.evidence)
    elif issue == "dataset": target.source_dataset = "other"
    elif issue == "claim_inferred": target.properties["fat_level"].evidence.status = "inferred"
    elif issue == "unexpected_sugar":
        from recipe_wrangler.services.adaptation.models import PropertyAssertion
        target.properties["sugar_level"] = PropertyAssertion(value="sugar_free", evidence=target.basis.evidence)
    elif issue == "tampered_change": edit.nutritional_change.to_value = "fat_free"
    result = compare_raw_part(baseline, edit, target)
    assert result.status != "supported_input_comparison"
    assert result.before is None


def test_no_added_sugar_is_not_treated_as_zero_sugar(products):
    source, target = products("Yogurt", "source", "sugar_g", 5.), products("Yogurt, no added sugar", "target", "sugar_g", 5.)
    edit = make_substitution_edit(snapshot(source), 0, target, nutritional_metric="sugar_g")
    assert edit.nutritional_change.to_value == "no_added_sugar"
    assert "nutritional_variant_has_no_numeric_improvement" in compare_raw_part(snapshot(source), edit, target).blockers


def test_variant_preserves_allergen_checks(products):
    source, target = products("Milk, whole", "source", "saturated_fat_g", 10.), products("Milk, skimmed", "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    result = compare_raw_part(baseline, edit, target, ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result.status == "needs_evidence" and result.before is None


def test_facet_shortlist_keeps_ten_best_numeric_candidates(products, monkeypatch):
    source = products("Soy sauce", "source", "sodium_mg", 2000.)
    targets = [products("Soy sauce, reduced sodium", str(i), "sodium_mg", 100. + i) for i in range(12)]
    by_id = {item.ref.candidate_id: item for item in targets}
    rows = [{**target.ref.model_dump(), "candidate": {"document": "Soy sauce, reduced sodium"}} for target in reversed(targets)]
    monkeypatch.setattr(candidate_selection, "nutritional_variant_records", lambda *args: rows)
    monkeypatch.setattr(candidate_selection, "_composition", lambda row: by_id[row["candidate_id"]])
    result = candidate_selection.shortlist_raw_part_candidates(snapshot(source), target_position=0,
        objective=NutrientObjective(metric="sodium_mg"), allow_general_substitutions=True, require_score_inputs=True)
    assert len(result.screening) == 12
    assert [ref.candidate_id for ref in result.candidate_refs] == [str(i) for i in range(10)]
    assert all(item.discovery_mechanism == "same_identity_nutritional_facet_variant" for item in result.screening)


def test_variant_patches_replace_the_old_claim_without_changing_quantities(products):
    source, target = products("Milk, whole", "source", "saturated_fat_g", 10.), products("Milk, skimmed", "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    baseline.ingredients[0].name = "whole milk"
    baseline.ingredients[0].original_text = "100 g whole milk"
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    context = build_adaptation_context(baseline, title="Milk dish", instructions=["Add the whole milk, then heat for 2 minutes."], objective="Reduce saturated fat", target_position=0)
    patches, texts = _allowed_patches(context, edit)
    for patch in sorted(patches, key=lambda p: p.start, reverse=True):
        key = patch.kind, patch.reference_id
        texts[key] = texts[key][:patch.start] + patch.after + texts[key][patch.end:]
    assert texts[("ingredient_text", "ingredient:0")] == "100 g skimmed milk"
    assert texts[("instruction", "step:0")] == "Add the skimmed milk, then heat for 2 minutes."


def test_numeric_fat_percentage_is_not_left_beside_the_new_claim(products):
    source, target = products("Milk, whole", "source", "saturated_fat_g", 10.), products("Milk, skimmed", "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    baseline.ingredients[0].original_text = "100 g milk, 3.5% fat"
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    context = build_adaptation_context(baseline, title="Milk dish", instructions=["Add the milk."], objective="Reduce saturated fat", target_position=0)
    with pytest.raises(ValueError, match="numeric_fat_claim_requires_explicit_patch"):
        _allowed_patches(context, edit)


@pytest.mark.parametrize("text", ["100 g milk (whole)", "100 g milk, whole", "100 g full cream milk"])
def test_variant_patches_remove_the_complete_old_claim(products, text):
    source, target = products("Milk, whole", "source", "saturated_fat_g", 10.), products("Milk, skimmed", "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    baseline.ingredients[0].original_text = text
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    context = build_adaptation_context(baseline, title="Milk dish", instructions=["Add the milk (whole)."], objective="Reduce saturated fat", target_position=0)
    patches, texts = _allowed_patches(context, edit)
    for patch in sorted(patches, key=lambda p: p.start, reverse=True):
        key = patch.kind, patch.reference_id
        texts[key] = texts[key][:patch.start] + patch.after + texts[key][patch.end:]
    assert texts[("ingredient_text", "ingredient:0")] == "100 g skimmed milk"
    assert texts[("instruction", "step:0")] == "Add the skimmed milk."


@pytest.mark.parametrize("text", ["100 g milk (whole, chilled)", "100 g whole fresh milk", "100 g low-fat milk"])
def test_variant_patches_abstain_on_unhandled_or_conflicting_claims(products, text):
    source, target = products("Milk, whole", "source", "saturated_fat_g", 10.), products("Milk, skimmed", "target", "saturated_fat_g", 1.)
    baseline = snapshot(source)
    baseline.ingredients[0].original_text = text
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="saturated_fat_g")
    context = build_adaptation_context(baseline, title="Milk dish", instructions=["Add the milk."], objective="Reduce saturated fat", target_position=0)
    with pytest.raises(ValueError, match="unresolved_nutritional_claim_reference"):
        _allowed_patches(context, edit)


def test_same_product_generic_instruction_reference_can_remain_unchanged(products):
    source, target = products("Soy sauce", "source", "sodium_mg", 1000.), products("Soy sauce, reduced sodium", "target", "sodium_mg", 100.)
    baseline = snapshot(source)
    edit = make_substitution_edit(baseline, 0, target, nutritional_metric="sodium_mg")
    context = build_adaptation_context(baseline, title="Sauce dish", instructions=["Add the sauce."], objective="Reduce sodium", target_position=0)
    patches, _ = _allowed_patches(context, edit)
    assert all(patch.kind != "instruction" for patch in patches)
    assert any(patch.kind == "ingredient_text" for patch in patches)


def test_facet_discovery_to_final_card_and_audit(products, monkeypatch):
    source, target = products("Soy sauce", "source", "sodium_mg", 2000.), products("Soy sauce, reduced sodium", "target", "sodium_mg", 100.)
    baseline = snapshot(source)
    rows = [{**target.ref.model_dump(), "candidate": {"document": "Soy sauce, reduced sodium"}}]
    monkeypatch.setattr(candidate_selection, "nutritional_variant_records", lambda *args: rows)
    monkeypatch.setattr(candidate_selection, "_composition", lambda row: target)
    calls = []
    def request(payload, response_model, config, **kwargs):
        calls.append(response_model.__name__)
        quotes = [{"kind": "ingredient", "reference_id": "ingredient:0", "quote": "soy sauce"},
            {"kind": "instruction", "reference_id": "step:0", "quote": "Mix the soy sauce into the dish."}]
        if response_model.__name__ == "RoleAssessment":
            return json.dumps({"status": "inferred", "roles": [{"role": "main_component", "reason": "Synthetic fixture", "evidence": quotes}], "uncertainties": []})
        if response_model.__name__ == "InstructionPatchPlan":
            return json.dumps({"status": "proposed", "patches": payload["allowed_patches"], "uncertainties": []})
        return json.dumps({"assessments": [{"candidate_id": item["candidate_id"], "verdict": "suitable", "reason": "Synthetic fixture", "role_fit": [{"role": "main_component", "status": "preserved"}], "evidence": quotes, "required_changes": [], "uncertainties": []} for item in payload["candidates"]]})
    for module in (contextual_assessment, instruction_editor):
        monkeypatch.setattr(module, "_request", request)
        monkeypatch.setattr(module, "_llm_config", lambda: {"source": "vllm", "model": "mock-facet-test"})
    score = InputScoreContext(recipe_id=baseline.recipe_id, baseline_run_id=baseline.run_id,
        quantity_input_hash=baseline.quantity_input_hash, basis="selected_state_input_estimate", total_weight_g=100.,
        food_type="solid", fruit_veg_legume_percent=0., evidence=source.basis.evidence)
    result = enhance_recipe(baseline, title="Sauce dish", instructions=["Mix the soy sauce into the dish."],
        mode="nutrition", nutri_score_breakdown=breakdown(sodium=10), score_context=score)
    assert result["status"] == "draft_requires_review", result["reasons"]
    assert len(calls) == 3
    assert result["adapted_recipe"]["ingredients"][0]["name"] == "reduced-sodium soy sauce"
    assert result["adapted_recipe"]["instructions"] == ["Mix the reduced-sodium soy sauce into the dish."]
    assert result["candidates"][0]["mechanism"] == "same_identity_nutritional_facet_variant"
    assert result["candidates"][0]["proposed_edit"]["nutritional_change"]["to_value"] == "reduced"
    assert result["adapted_recipe"]["nutrition"]["per_serving"]["sodium_mg"] == 50.
    assert not result["saved"] and not result["recipe_ready"]



def test_cross_table_variant_is_conditional_and_requires_estimate_policy(products):
    source=products('Milk, whole','source','saturated_fat_g',10.)
    target=products('Milk, low-fat','target','saturated_fat_g',1.)
    target.source_dataset='other released table'
    baseline=snapshot(source)
    edit=make_substitution_edit(baseline,0,target,nutritional_metric='saturated_fat_g')
    result=compare_raw_part(baseline,edit,target)
    assert result.status=='supported_input_comparison'
    assert any('across released food-composition tables' in text for text in result.uncertainty)
    baseline.input_policy='verified'
    assert 'cross_source_comparison_not_supported' in compare_raw_part(baseline,edit,target).blockers
