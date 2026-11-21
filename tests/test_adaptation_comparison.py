"""Synthetic evidence fixtures verify comparison mechanics, not food benefits."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import (
    ComparisonComposition, ComparisonConstraints, ComparisonIngredient,
    ComparisonQuantity, ComparisonSnapshot, PropertyAssertion, QuantityBasisDeclaration, RawPartEdit,
)
from recipe_wrangler.tools.nutrition_completeness import METRICS


RAW_CHICKEN_PROPERTIES = {"base_identity": "chicken", "biological_source": "chicken",
    "preparation_state": "raw", "skin_status": "skinless", "bone_status": "boneless",
    "processes": [], "preservation_methods": [], "qualifiers.coating": "none",
    "qualifiers.added_fat": "none", "qualifiers.added_salt": False, "qualifiers.added_ingredients": []}


def evidence(status="stated"):
    return {"reference": "synthetic-fixture", "method": "test_fixture", "version": "1", "status": status}


def fact(value, status="stated"):
    return {"value": value, "evidence": evidence(status)}


def composition(part="thigh", **changes):
    data = {
        "ref": {"release_id": "release-1", "collection": "nutritional_ingredients_eu", "candidate_id": part},
        "source_dataset": "synthetic", "source_release": "1",
        "basis": {"amount_g": 100, "portion": "edible", "evidence": evidence()},
        "properties": {key: fact(value) for key, value in {**RAW_CHICKEN_PROPERTIES, "food_part": part}.items()},
        "values_per_100g": {metric: 0 for metric in METRICS},
        "nutrient_definitions": {metric: "synthetic:" + metric for metric in METRICS},
    }
    data["values_per_100g"].update(energy_kcal=200 if part == "thigh" else 120, protein_g=20)
    data.update(changes)
    return ComparisonComposition.model_validate(data)


@pytest.fixture
def inputs():
    original, target = composition(), composition("breast")
    ingredient = ComparisonIngredient(
        position=0, name="chicken", original_text="200 g raw boneless skinless chicken thigh",
        quantity=ComparisonQuantity(weight_g=200, basis="raw_edible", evidence=evidence()),
        observed_properties=deepcopy(original.properties), composition=original,
    )
    snapshot = ComparisonSnapshot(
        recipe_id="recipe-1", run_id="run-1", nutrition_source="irish", release_id="release-1",
        quantity_input_hash="quantities-1", serves=2, ingredients=[ingredient],
    )
    edit = RawPartEdit(
        source_identity="chicken", target_identity="chicken", source_biological_source="chicken",
        target_biological_source="chicken", source_food_part="thigh", target_food_part="breast",
        recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, position=0,
        from_composition=original.ref.model_copy(deep=True), to_composition=target.ref.model_copy(deep=True),
    )
    return snapshot, edit, target


def test_raw_edible_comparison_uses_supported_amounts_and_preserves_inputs(inputs):
    snapshot, edit, target = inputs
    before_inputs = [obj.model_dump() for obj in inputs]
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "supported_input_comparison"
    assert result.before["complete_totals"]["energy_kcal"] == 400
    assert result.after["complete_totals"]["energy_kcal"] == 240
    assert result.ingredient_delta["energy_kcal"] == -160
    assert result.recipe_delta_per_serving["energy_kcal"] == -80
    assert result.ingredient_delta["sodium_mg"] == 0
    assert result.instruction_status == "pending"
    assert result.culinary_status == "not_evaluated"
    assert result.quantity.weight_g == 200
    assert result.observed_properties["food_part"].value == "thigh"
    assert result.target_composition.properties["food_part"].value == "breast"
    result.target_composition.properties["food_part"].value = "changed result"
    assert "nutri_score" not in result.after
    assert [obj.model_dump() for obj in inputs] == before_inputs


def test_occurrence_position_changes_only_one_of_two_identical_names(inputs):
    snapshot, edit, target = inputs
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position = 3
    other.quantity.weight_g = 100
    snapshot.ingredients.append(other)
    result = compare_raw_part(snapshot, edit, target)
    assert result.before["complete_totals"]["energy_kcal"] == 600
    assert result.after["complete_totals"]["energy_kcal"] == 440
    assert snapshot.ingredients[1].composition.ref.candidate_id == "thigh"


def test_unknown_target_nutrient_remains_unknown_and_other_deltas_survive(inputs):
    snapshot, edit, target = inputs
    target.values_per_100g["sodium_mg"] = None
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "supported_input_comparison"
    assert result.after["complete_totals"]["sodium_mg"] is None
    assert result.after["supported_partial_totals"]["sodium_mg"] == 0
    assert result.after["missing_contributions"]["sodium_mg"] == 1
    assert result.ingredient_delta["sodium_mg"] is None
    assert result.recipe_delta["sodium_mg"] is None
    assert result.ingredient_delta["energy_kcal"] == -160


def test_unresolved_unrelated_ingredient_does_not_become_zero(inputs):
    snapshot, edit, target = inputs
    snapshot.ingredients.append(ComparisonIngredient(
        position=1, name="unresolved sauce", quantity=ComparisonQuantity(
            weight_g=50, basis="unknown", evidence=evidence("inferred")),
    ))
    result = compare_raw_part(snapshot, edit, target)
    assert result.before["complete_totals"]["energy_kcal"] is None
    assert result.after["supported_partial_totals"]["energy_kcal"] == 240
    assert result.ingredient_delta["energy_kcal"] == -160
    assert result.recipe_delta["energy_kcal"] is None
    assert result.recipe_delta_per_serving["energy_kcal"] is None


def test_missing_servings_does_not_default_to_one(inputs):
    snapshot, edit, target = inputs
    snapshot.serves = None
    result = compare_raw_part(snapshot, edit, target)
    assert result.recipe_delta["energy_kcal"] == -160
    assert result.recipe_delta_per_serving["energy_kcal"] is None
    assert result.after["complete_totals_per_serving"]["energy_kcal"] is None


@pytest.mark.parametrize("field,value,reason", [
    ("baseline_run_id", "stale", "baseline_mismatch"),
    ("recipe_id", "another-recipe", "baseline_mismatch"),
    ("quantity_input_hash", "changed-quantities", "baseline_mismatch"),
    ("position", 9, "occurrence_not_found"),
])
def test_edit_must_identify_the_pinned_occurrence(inputs, field, value, reason):
    snapshot, edit, target = inputs
    setattr(edit, field, value)
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "blocked"
    assert reason in result.blockers
    assert result.after is None


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("field,value,reason", [
    ("release_id", "other", "release_mismatch"),
    ("collection", "nutritional_ingredients_hungarian", "region_mismatch"),
])
def test_release_and_regional_scope_are_checked(inputs, side, field, value, reason):
    snapshot, edit, target = inputs
    item = target if side == "target" else snapshot.ingredients[0].composition
    setattr(item.ref, field, value)
    if side == "target":
        edit.to_composition = item.ref.model_copy(deep=True)
    else:
        edit.from_composition = item.ref.model_copy(deep=True)
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "blocked"
    assert side + "." + reason in result.blockers


@pytest.mark.parametrize("key,value", [
    ("preparation_state", "cooked"), ("biological_source", "turkey"),
    ("skin_status", "with_skin"), ("bone_status", "bone_in"),
    ("qualifiers.coating", "breaded"), ("qualifiers.added_salt", True),
    ("qualifiers.added_fat", "oil"), ("qualifiers.added_ingredients", ["milk"]),
])
def test_graph_related_but_incompatible_target_is_blocked(inputs, key, value):
    snapshot, edit, target = inputs
    target.properties[key].value = value
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "blocked"
    assert "target.property_conflict:" + key in result.blockers


@pytest.mark.parametrize("side", ["observed", "source", "target"])
@pytest.mark.parametrize("state", ["missing", "inferred", "proxy"])
def test_required_properties_need_independent_stated_evidence(inputs, side, state):
    snapshot, edit, target = inputs
    properties = {"observed": snapshot.ingredients[0].observed_properties,
                  "source": snapshot.ingredients[0].composition.properties,
                  "target": target.properties}[side]
    if state == "missing":
        del properties["skin_status"]
    else:
        properties["skin_status"].evidence.status = state
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "needs_evidence"
    assert side + ".property:skin_status" in result.missing_evidence
    assert result.after is None


def test_new_or_lost_properties_require_evidence_and_other_known_properties_are_preserved(inputs):
    snapshot, edit, target = inputs
    target.properties["physical_form"] = PropertyAssertion.model_validate(fact("minced"))
    assert "preserved_property:physical_form" in compare_raw_part(snapshot, edit, target).missing_evidence
    snapshot.ingredients[0].observed_properties["physical_form"] = PropertyAssertion.model_validate(fact("whole"))
    snapshot.ingredients[0].composition.properties["physical_form"] = PropertyAssertion.model_validate(fact("whole"))
    assert "preserved_property_conflict:physical_form" in compare_raw_part(snapshot, edit, target).blockers


@pytest.mark.parametrize("basis,status", [("as_purchased", "stated"), ("cooked_edible", "stated"), ("raw_edible", "proxy")])
def test_equal_grams_are_not_enough_without_raw_edible_mass(inputs, basis, status):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.basis = basis
    snapshot.ingredients[0].quantity.evidence.status = status
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "needs_evidence"
    assert "occurrence.raw_edible_mass" in result.missing_evidence


def test_incompatible_nutrient_definition_withholds_only_that_delta(inputs):
    snapshot, edit, target = inputs
    target.nutrient_definitions["carbs_g"] = "different carbohydrate definition"
    result = compare_raw_part(snapshot, edit, target)
    assert result.ingredient_delta["carbs_g"] is None
    assert result.ingredient_delta["energy_kcal"] == -160
    assert result.nutrient_blockers["carbs_g"] == ["incompatible_nutrient_definition"]


@pytest.mark.parametrize("kind,name,value,expected", [
    ("allergen", "milk", "present", "blocked"),
    ("allergen", "milk", "unknown", "needs_evidence"),
    ("allergen", "milk", "absent", "supported_input_comparison"),
    ("consumer", "vegan", "not_suitable", "blocked"),
])
def test_hard_constraints_use_explicit_evidence(inputs, kind, name, value, expected):
    snapshot, edit, target = inputs
    if kind == "allergen":
        target.allergen_assessments[name] = PropertyAssertion.model_validate(fact(value))
        constraints = ComparisonConstraints(forbidden_allergens={name})
    else:
        target.consumer_assessments[name] = PropertyAssertion.model_validate(fact(value))
        constraints = ComparisonConstraints(required_consumer_groups={name})
    result = compare_raw_part(snapshot, edit, target, constraints)
    assert result.status == expected


def test_unknown_constraint_evidence_on_unchanged_ingredient_blocks_approval(inputs):
    snapshot, edit, target = inputs
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position = 1
    snapshot.ingredients.append(other)
    result = compare_raw_part(snapshot, edit, target, ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result.status == "needs_evidence"
    assert "occurrence:1.allergen:milk" in result.missing_evidence


@pytest.mark.parametrize("weight", [-1, float("nan"), float("inf"), True, "200"])
def test_invalid_masses_are_rejected_at_the_boundary(weight):
    with pytest.raises(ValidationError):
        ComparisonQuantity(weight_g=weight, basis="raw_edible", evidence=evidence())


def test_invalid_nutrient_values_and_duplicate_positions_are_rejected(inputs):
    snapshot, edit, target = inputs
    data = target.model_dump()
    data["values_per_100g"]["sodium_mg"] = -1
    with pytest.raises(ValidationError):
        ComparisonComposition.model_validate(data)
    data = snapshot.model_dump()
    data["ingredients"].append(deepcopy(data["ingredients"][0]))
    with pytest.raises(ValidationError, match="positions must be unique"):
        ComparisonSnapshot.model_validate(data)


def test_all_missing_nutrients_cannot_claim_supported_comparison(inputs):
    snapshot, edit, target = inputs
    target.values_per_100g.clear()
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "needs_evidence"
    assert "no_comparable_nutrients" in result.missing_evidence


@pytest.mark.parametrize("side", ["source", "target"])
def test_exact_selected_composition_must_match_the_edit(inputs, side):
    snapshot, edit, target = inputs
    reference = edit.from_composition if side == "source" else edit.to_composition
    reference.candidate_id = "different-record"
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "blocked"
    assert side + "_composition_mismatch" in result.blockers


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("change,expected", [
    ("amount", "blocked"), ("portion", "blocked"),
    ("proxy", "needs_evidence"), ("blocker", "blocked"),
])
def test_source_nutrient_basis_must_be_supported(inputs, side, change, expected):
    snapshot, edit, target = inputs
    item = target if side == "target" else snapshot.ingredients[0].composition
    if change == "amount":
        item.basis.amount_g = 1
    elif change == "portion":
        item.basis.portion = "as_purchased"
    elif change == "proxy":
        item.basis.evidence.status = "proxy"
    else:
        item.blockers.append("missing_exact_source_linkage")
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == expected
    assert result.after is None


def test_different_source_tables_cannot_be_assumed_equivalent(inputs):
    snapshot, edit, target = inputs
    target.source_dataset = "another-table"
    assert "cross_source_comparison_not_supported" in compare_raw_part(snapshot, edit, target).blockers


@pytest.mark.parametrize("change", ["cooked", "wrong_release", "wrong_region", "definition", "proxy_mass"])
def test_unsupported_unchanged_row_prevents_a_complete_total(inputs, change):
    snapshot, edit, target = inputs
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position = 1
    if change == "cooked":
        other.composition.properties["preparation_state"].value = "cooked"
    elif change == "wrong_release":
        other.composition.ref.release_id = "another-release"
    elif change == "wrong_region":
        other.composition.ref.collection = "nutritional_ingredients_slovenian"
    elif change == "definition":
        other.composition.nutrient_definitions["energy_kcal"] = "another definition"
    else:
        other.quantity.evidence.status = "proxy"
    snapshot.ingredients.append(other)
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "supported_input_comparison"
    assert result.after["complete_totals"]["energy_kcal"] is None
    assert result.after["supported_partial_totals"]["energy_kcal"] == 240
    assert result.recipe_delta["energy_kcal"] is None
    assert result.ingredient_delta["energy_kcal"] == -160


def test_constraint_evidence_cannot_be_borrowed_from_original_or_inferred(inputs):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].composition.allergen_assessments["milk"] = PropertyAssertion.model_validate(fact("absent"))
    constraints = ComparisonConstraints(forbidden_allergens={"milk"})
    assert compare_raw_part(snapshot, edit, target, constraints).status == "needs_evidence"
    target.allergen_assessments["milk"] = PropertyAssertion.model_validate(fact("absent", "inferred"))
    assert compare_raw_part(snapshot, edit, target, constraints).status == "needs_evidence"
    target.allergen_assessments["milk"] = PropertyAssertion.model_validate(fact([]))
    assert compare_raw_part(snapshot, edit, target, constraints).status == "needs_evidence"


def test_known_incompatible_diet_cannot_be_overridden_by_a_suitability_assertion(inputs):
    snapshot, edit, target = inputs
    target.consumer_assessments["vegan"] = PropertyAssertion.model_validate(fact("suitable"))
    result = compare_raw_part(snapshot, edit, target, ComparisonConstraints(required_consumer_groups={"vegan"}))
    assert "consumer_group_conflict:vegan" in result.blockers


def test_unassessed_definition_is_not_comparability_evidence(inputs):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].composition.nutrient_definitions["carbs_g"] = "unknown"
    target.nutrient_definitions["carbs_g"] = "unknown"
    result = compare_raw_part(snapshot, edit, target)
    assert result.ingredient_delta["carbs_g"] is None
    assert result.after["complete_totals"]["carbs_g"] is None
    assert result.nutrient_blockers["carbs_g"] == ["unknown_nutrient_definition"]


def test_small_changed_definition_gap_does_not_erase_unchanged_production_inputs(inputs):
    snapshot, edit, target = inputs
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position = 1
    other.quantity.weight_g = 1000
    other.composition.values_per_100g['carbs_g'] = 10
    snapshot.ingredients.append(other)
    snapshot.input_policy = 'production_estimate'
    snapshot.ingredients[0].quantity.weight_g = 1
    snapshot.ingredients[0].composition.nutrient_definitions.pop('carbs_g')
    target.nutrient_definitions.pop('carbs_g')
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == 'supported_input_comparison'
    assert result.recipe_delta_per_serving['carbs_g'] is None
    assert result.after['complete_totals']['carbs_g'] == 100
    assert result.after['nutrient_status']['carbs_g'] == 'minor_gaps'
    assert result.after['ingredient_gaps'][0]['position'] == 0


def test_finite_inputs_cannot_leak_overflowed_totals(inputs):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.weight_g = 1e308
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "blocked"
    assert result.blockers == ["nonfinite_nutrient_calculation"]
    assert result.before is result.after is None
    assert not result.ingredient_delta
    result.model_dump_json()


def test_zero_weight_is_not_an_implicit_removal_operation(inputs):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.weight_g = 0
    assert "excluded_occurrence" in compare_raw_part(snapshot, edit, target).blockers


@pytest.mark.parametrize("change", [
    {"operation": "CHANGE_COOKING_METHOD"}, {"target_food_part": ""},
    {"position": -1}, {"quantity_policy": "preserve_cooked_mass"},
    {"unapproved_ingredient": "oil"},
])
def test_unsupported_operations_are_rejected_instead_of_silently_ignored(inputs, change):
    snapshot, edit, target = inputs
    with pytest.raises(ValidationError):
        RawPartEdit.model_validate({**edit.model_dump(), **change})


def test_explicit_other_food_part_still_requires_matching_target_facts(inputs):
    snapshot, edit, target = inputs
    edit.target_food_part = "wing"
    assert compare_raw_part(snapshot, edit, target).status == "blocked"
    target.properties["food_part"].value = "wing"
    assert compare_raw_part(snapshot, edit, target).status == "supported_input_comparison"


@pytest.fixture
def quantity_declaration(inputs):
    snapshot, edit, target = inputs
    return QuantityBasisDeclaration(
        recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, position=0, weight_g=200,
        basis="raw_edible", declared_by="synthetic-reviewer",
        statement="The saved 200 g measures the raw edible ingredient, after removing skin and bones.",
        evidence={"reference": "synthetic-review/1", "method": "source_review", "version": "1"},
    )


def test_declared_basis_enables_a_conditional_comparison_without_rewriting_observations(inputs, quantity_declaration):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.basis = "unknown"
    del snapshot.ingredients[0].observed_properties["preparation_state"]
    before = snapshot.model_dump()
    declaration_before = quantity_declaration.model_dump()
    assert compare_raw_part(snapshot, edit, target).status == "needs_evidence"
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.status == "supported_input_comparison"
    assert result.ingredient_delta["energy_kcal"] == -160
    assert result.recipe_delta_per_serving["energy_kcal"] == -80
    assert result.baseline_quantity.basis == "unknown"
    assert result.quantity.basis == "raw_edible"
    assert result.quantity.weight_g == result.baseline_quantity.weight_g == 200
    assert "preparation_state" not in result.observed_properties
    assert result.quantity.evidence.method == "source_review"
    assert result.quantity_declarations[0].declared_by == "synthetic-reviewer"
    assert any("conditional" in message for message in result.uncertainty)
    assert result.culinary_status == "not_evaluated" and result.instruction_status == "pending"
    assert snapshot.model_dump() == before
    result.quantity_declarations[0].statement = "changed output"
    assert quantity_declaration.model_dump() == declaration_before
    result.model_dump_json()


@pytest.mark.parametrize("field,value,reason", [
    ("recipe_id", "other", "quantity_declaration_baseline_mismatch"),
    ("baseline_run_id", "old-run", "quantity_declaration_baseline_mismatch"),
    ("quantity_input_hash", "old-quantities", "quantity_declaration_baseline_mismatch"),
    ("position", 99, "quantity_declaration_occurrence_not_found"),
    ("weight_g", 150.0, "quantity_declaration_mass_mismatch"),
    ("weight_g", 200.0000000001, "quantity_declaration_mass_mismatch"),
])
def test_declarations_cannot_target_other_inputs_or_change_mass(inputs, quantity_declaration, field, value, reason):
    snapshot, edit, target = inputs
    setattr(quantity_declaration, field, value)
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.status == "blocked" and result.blockers == [reason]
    assert result.before is result.after is None


@pytest.mark.parametrize("basis", ["as_purchased", "cooked_edible", "drained", "reconstituted"])
def test_declaration_cannot_stand_in_for_a_conversion(inputs, quantity_declaration, basis):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.basis = basis
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.blockers == ["quantity_declaration_requires_conversion"]
    assert snapshot.ingredients[0].quantity.basis == basis


def test_declaration_cannot_override_observed_cooked_state(inputs, quantity_declaration):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.basis = "unknown"
    snapshot.ingredients[0].observed_properties["preparation_state"].value = "cooked"
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.blockers == ["quantity_declaration_state_conflict"]


def test_duplicate_declarations_are_not_applied_in_arbitrary_order(inputs, quantity_declaration):
    snapshot, edit, target = inputs
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration, quantity_declaration])
    assert result.blockers == ["duplicate_quantity_declaration"]


@pytest.mark.parametrize("change", [
    {"weight_g": 0}, {"weight_g": float("nan")}, {"weight_g": "200"},
    {"basis": "cooked_edible"}, {"declared_by": " "}, {"statement": ""},
    {"evidence": {"reference": "x", "method": "llm", "version": "1"}},
    {"evidence": {"reference": "x", "method": "source_review", "version": "1", "status": "inferred"}},
    {"evidence": {"reference": "x", "method": "user_confirmation", "version": "1", "status": "proxy"}},
    {"approve_composition": True},
])
def test_declaration_schema_requires_explicit_scoped_attestation(quantity_declaration, change):
    with pytest.raises(ValidationError):
        QuantityBasisDeclaration.model_validate({**quantity_declaration.model_dump(), **change})


def test_mutated_declaration_is_revalidated_before_use(inputs, quantity_declaration):
    snapshot, edit, target = inputs
    quantity_declaration.evidence.method = "llm"
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.blockers == ["invalid_quantity_declaration"]


def test_each_unknown_occurrence_needs_its_own_declaration_for_complete_totals(inputs, quantity_declaration):
    snapshot, edit, target = inputs
    other = snapshot.ingredients[0].model_copy(deep=True)
    other.position = 1
    snapshot.ingredients.append(other)
    for item in snapshot.ingredients:
        item.quantity.basis = "unknown"
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration])
    assert result.status == "supported_input_comparison"
    assert result.ingredient_delta["energy_kcal"] == -160
    assert result.after["complete_totals"]["energy_kcal"] is None
    other_declaration = quantity_declaration.model_copy(update={"position": 1})
    result = compare_raw_part(snapshot, edit, target, quantity_declarations=[quantity_declaration, other_declaration])
    assert result.before["complete_totals"]["energy_kcal"] == 800
    assert result.after["complete_totals"]["energy_kcal"] == 640
    assert result.recipe_delta_per_serving["energy_kcal"] == -80


@pytest.mark.parametrize("issue", ["source_basis", "missing_skin", "added_salt", "allergen", "source_table"])
def test_quantity_declaration_cannot_approve_other_substitution_requirements(inputs, quantity_declaration, issue):
    snapshot, edit, target = inputs
    snapshot.ingredients[0].quantity.basis = "unknown"
    constraints = ComparisonConstraints()
    if issue == "source_basis":
        target.basis.evidence.status = "proxy"
    elif issue == "missing_skin":
        del snapshot.ingredients[0].observed_properties["skin_status"]
    elif issue == "added_salt":
        target.properties["qualifiers.added_salt"].value = True
    elif issue == "allergen":
        constraints.forbidden_allergens = {"milk"}
    else:
        target.source_dataset = "other"
    result = compare_raw_part(snapshot, edit, target, constraints, quantity_declarations=[quantity_declaration])
    assert result.status in {"blocked", "needs_evidence"}
    assert result.after is None
