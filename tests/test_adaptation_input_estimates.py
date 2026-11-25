"""Conditional production estimates preserve uncertainty and material conflicts."""

from copy import deepcopy

import pytest

from test_adaptation_comparison import inputs, evidence, fact
from test_adaptation_comparison_store import database
from recipe_wrangler.services.adaptation.comparison import compare_raw_part
from recipe_wrangler.services.adaptation.models import ComparisonConstraints, ComparisonQuantity, PropertyAssertion
from recipe_wrangler.services.adaptation.comparison_store import load_active_snapshot
from recipe_wrangler.services.adaptation.enhancement import plan_enhancement
from recipe_wrangler.services.adaptation.production_candidates import make_substitution_edit
from recipe_wrangler.services.adaptation.production_candidates import _candidate_profile


@pytest.fixture
def estimated(inputs):
    snapshot, edit, target = inputs
    snapshot.input_policy = "production_estimate"
    snapshot.ingredients[0].quantity = ComparisonQuantity(weight_g=200, basis="selected_state_edible",
        evidence={**evidence("proxy"), "method": "saved_production_input_estimate"})
    for properties in (snapshot.ingredients[0].observed_properties, snapshot.ingredients[0].composition.properties, target.properties):
        for key in list(properties):
            if key.startswith("qualifiers.") or key in {"processes", "preservation_methods"}:
                del properties[key]
    return inputs


def test_loader_opt_in_retains_proxy_without_mutating_stored_quantities(database):
    active = deepcopy(database[0])
    snapshot = load_active_snapshot("recipe-1", "irish", allow_input_estimates=True)
    assert snapshot.input_policy == "production_estimate"
    assert snapshot.ingredients[0].quantity.basis == "selected_state_edible"
    assert snapshot.ingredients[0].quantity.evidence.status == "proxy"
    assert database[0] == active
    assert load_active_snapshot("recipe-1", "irish").ingredients[0].quantity.basis == "unknown"


def test_optional_unknown_properties_do_not_become_absence_facts(estimated):
    result = compare_raw_part(*estimated)
    assert result.status == "supported_input_comparison"
    assert result.quantity_basis == "selected_state_input_estimate"
    assert result.quantity.evidence.status == "proxy"
    assert "qualifiers.coating" not in result.target_composition.properties
    assert any("remain unknown" in text for text in result.uncertainty)
    assert result.after["complete_totals"]["energy_kcal"] == 240


@pytest.mark.parametrize("label", ["observed", "source", "target"])
def test_cooked_substitutions_still_require_a_different_execution_mechanism(estimated, label):
    snapshot, edit, target = estimated
    properties = {"observed": snapshot.ingredients[0].observed_properties,
        "source": snapshot.ingredients[0].composition.properties, "target": target.properties}[label]
    properties["preparation_state"].value = "cooked"
    result = compare_raw_part(*estimated)
    assert result.status == "blocked"
    assert result.before is None


def test_known_coating_cannot_disappear_into_unknown(estimated):
    estimated[0].ingredients[0].observed_properties["qualifiers.coating"] = PropertyAssertion.model_validate(fact("breaded"))
    result = compare_raw_part(*estimated)
    assert result.status == "needs_evidence"
    assert "preserved_property:qualifiers.coating" in result.missing_evidence


def test_same_cooked_state_can_be_compared_without_raw_conversion(estimated):
    snapshot, edit, target = estimated
    for properties in (snapshot.ingredients[0].observed_properties,
                       snapshot.ingredients[0].composition.properties, target.properties):
        properties['preparation_state'].value = 'cooked'
    result = compare_raw_part(*estimated)
    assert result.status == 'supported_input_comparison'
    assert result.quantity.weight_g == snapshot.ingredients[0].quantity.weight_g


def test_cooked_state_does_not_erase_known_cooking_method_conflicts(estimated):
    snapshot, edit, target = estimated
    for properties in (snapshot.ingredients[0].observed_properties,
                       snapshot.ingredients[0].composition.properties, target.properties):
        properties['preparation_state'].value = 'cooked'
        properties['processes'] = PropertyAssertion.model_validate(fact(['boiled']))
    target.properties['processes'].value = ['fried']
    result = compare_raw_part(*estimated)
    assert result.status == 'blocked'
    assert 'preserved_property_conflict:processes' in result.blockers


def test_unspecified_fortification_is_disclosed_not_preserved(estimated):
    snapshot, edit, target = estimated
    snapshot.ingredients[0].composition.properties['fortification'] = PropertyAssertion.model_validate(fact('vitamin_d'))
    result = compare_raw_part(*estimated)
    assert result.status == 'supported_input_comparison'
    assert any('Fortification' in text for text in result.uncertainty)
    snapshot.ingredients[0].observed_properties['fortification'] = PropertyAssertion.model_validate(fact('vitamin_d'))
    assert 'preserved_property:fortification' in compare_raw_part(*estimated).missing_evidence


def test_explicit_material_conflicts_remain_blocked(estimated):
    estimated[2].properties["bone_status"].value = "bone_in"
    result = compare_raw_part(*estimated)
    assert result.status == "blocked"
    assert "preserved_property_conflict:bone_status" in result.blockers


def test_unchanged_prepared_ingredient_uses_its_selected_state(estimated):
    snapshot, edit, target = estimated
    sauce = snapshot.ingredients[0].model_copy(deep=True)
    sauce.position = 1
    sauce.name = "prepared sauce"
    sauce.composition.properties["preparation_state"].value = "cooked"
    sauce.composition.nutrient_definitions = {
        key: "other-source:" + value for key, value in sauce.composition.nutrient_definitions.items()}
    snapshot.ingredients.append(sauce)
    result = compare_raw_part(*estimated)
    assert result.status == "supported_input_comparison"
    assert result.before["complete_totals"]["energy_kcal"] == 800
    assert result.after["complete_totals"]["energy_kcal"] == 640
    sauce.composition.values_per_100g["fibre_g"] = None
    result = compare_raw_part(*estimated)
    assert result.after["complete_totals"]["fibre_g"] is None


def test_unchanged_unknown_definition_keeps_input_values_without_authorizing_changed_unknowns(estimated):
    snapshot, edit, target = estimated
    unchanged = snapshot.ingredients[0].model_copy(deep=True)
    unchanged.position = 1
    unchanged.composition.values_per_100g['carbs_g'] = 10.0
    unchanged.composition.nutrient_definitions.pop('carbs_g')
    snapshot.ingredients.append(unchanged)
    result = compare_raw_part(*estimated)
    assert result.before['complete_totals']['carbs_g'] == 20.0
    assert result.after['complete_totals']['carbs_g'] == 20.0
    assert result.recipe_delta['carbs_g'] == 0.0
    assert any('1:carbs_g' in text and 'conditional input totals' in text for text in result.uncertainty)
    assert type(result).model_validate(result.model_dump()) == result
    target.nutrient_definitions.pop('carbs_g')
    result = compare_raw_part(*estimated)
    assert result.after['complete_totals']['carbs_g'] is None
    assert result.recipe_delta['carbs_g'] is None
    assert result.nutrient_blockers['carbs_g'] == ['unknown_nutrient_definition']
    target.nutrient_definitions['carbs_g'] = 'another analytical definition'
    assert compare_raw_part(*estimated).recipe_delta['carbs_g'] is None


def test_arbitrary_proxy_method_cannot_authorize_quantity(estimated):
    estimated[0].ingredients[0].quantity.evidence.method = "untrusted_guess"
    assert "occurrence.raw_edible_mass" in compare_raw_part(*estimated).missing_evidence


def test_allergen_unknown_still_prevents_a_draft(estimated):
    result = compare_raw_part(*estimated, constraints=ComparisonConstraints(forbidden_allergens={"milk"}))
    assert result.status == "needs_evidence" and result.before is None


def test_generic_plant_substitution_does_not_invent_anatomical_parts(estimated):
    snapshot, _, target = estimated
    original = snapshot.ingredients[0]
    for properties, identity in ((original.observed_properties, "peanut"), (original.composition.properties, "peanut"), (target.properties, "almond")):
        for key in ("skin_status", "bone_status", "food_part", "biological_source"):
            properties.pop(key, None)
        properties["base_identity"].value = identity
    edit = make_substitution_edit(snapshot, 0, target)
    assert edit.source_food_part is edit.target_food_part is None
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == "supported_input_comparison"


def test_strict_policy_is_not_implicitly_relaxed(estimated):
    estimated[0].input_policy = "verified"
    assert compare_raw_part(*estimated).status == "needs_evidence"


def test_candidate_resolution_uses_baseline_release_regardless_of_legacy_mode(estimated, monkeypatch):
    from unittest.mock import Mock
    from recipe_wrangler.matching import runtime
    from recipe_wrangler.services.adaptation import service
    resolver = Mock()
    resolver.calculate.return_value = {"details": [{"catalogue_release": "release-1"}]}
    engine = Mock(return_value=resolver)
    monkeypatch.setattr(runtime, "engine", engine)
    monkeypatch.setattr(service, "_fetch_candidate_profile", lambda *args: pytest.fail("Legacy matcher used"))
    monkeypatch.setenv("NUTRITION_MATCHER_MODE", "legacy")
    assert _candidate_profile(estimated[0], "chicken breast")["catalogue_release"] == "release-1"
    engine.assert_called_once_with("release-1")
    assert resolver.calculate.call_args.kwargs["source"] == "irish"


def test_raw_cutting_is_retained_without_a_separate_composition_record(estimated):
    estimated[0].ingredients[0].observed_properties["processes"] = PropertyAssertion.model_validate(fact(["diced"]))
    result = compare_raw_part(*estimated)
    assert result.status == "supported_input_comparison"
    assert result.observed_properties["processes"].value == ["diced"]


def test_conflicting_observed_species_cannot_be_overridden_by_selected_food(estimated):
    estimated[0].ingredients[0].observed_properties["biological_source"].value = "pig"
    assert compare_raw_part(*estimated).status == "blocked"


def test_bone_in_purchase_masses_need_edible_yield_even_in_estimate_mode(estimated):
    snapshot, edit, target = estimated
    for properties in (snapshot.ingredients[0].observed_properties,
                       snapshot.ingredients[0].composition.properties, target.properties):
        properties["bone_status"].value = "bone_in"
    result = compare_raw_part(*estimated)
    assert result.status == "needs_evidence" and result.before is None
    assert "target.edible_yield_required" in result.missing_evidence


@pytest.mark.parametrize('property_name', ['fat_level','fat_content_description','meat_fat_description'])
def test_leaner_cut_can_change_recipe_unspecified_fat_description(estimated,property_name):
    snapshot,edit,target=estimated
    source=snapshot.ingredients[0].composition
    source.properties[property_name]=PropertyAssertion.model_validate(fact('source fat description'))
    source.values_per_100g['saturated_fat_g']=10
    target.values_per_100g['saturated_fat_g']=2
    result=compare_raw_part(*estimated)
    assert result.status=='supported_input_comparison'
    assert result.recipe_delta_per_serving['saturated_fat_g']==-8
    assert any(property_name in text and 'equivalence is not claimed' in text for text in result.uncertainty)
    assert property_name not in target.properties


@pytest.mark.parametrize('observed,after', [(True,2),(False,10),(False,None)])
def test_fat_description_is_not_dropped_without_clear_conditional_support(estimated,observed,after):
    snapshot,edit,target=estimated
    source=snapshot.ingredients[0].composition
    source.properties['meat_fat_description']=PropertyAssertion.model_validate(fact('lean and fat'))
    if observed:snapshot.ingredients[0].observed_properties['meat_fat_description']=source.properties['meat_fat_description'].model_copy(deep=True)
    source.values_per_100g['saturated_fat_g']=10
    target.values_per_100g['saturated_fat_g']=after
    result=compare_raw_part(*estimated)
    assert result.status=='needs_evidence'
    assert 'preserved_property:meat_fat_description' in result.missing_evidence


def test_verified_comparisons_keep_fat_description_support_requirement(inputs):
    source=inputs[0].ingredients[0].composition
    source.properties['meat_fat_description']=PropertyAssertion.model_validate(fact('lean and fat'))
    source.values_per_100g['saturated_fat_g']=10
    inputs[2].values_per_100g['saturated_fat_g']=2
    assert 'preserved_property:meat_fat_description' in compare_raw_part(*inputs).missing_evidence


def as_described_pair(estimated,source_identity,target_identity):
    snapshot,edit,target=estimated
    item=snapshot.ingredients[0]
    edit.operation='REPLACE_INGREDIENT'
    edit.source_identity,edit.target_identity=source_identity,target_identity
    item.name,item.original_text=source_identity,'200g '+source_identity
    for properties,identity in [(item.observed_properties,source_identity),
            (item.composition.properties,source_identity),(target.properties,target_identity)]:
        properties['base_identity']=PropertyAssertion.model_validate(fact(identity))
        properties.pop('preparation_state',None)
    return estimated


@pytest.mark.parametrize('source,target',[('cream','yogurt'),('olive oil','rapeseed oil'),('sugar','honey')])
def test_as_described_nonstaple_families_can_be_compared_conditionally(estimated,source,target):
    result=compare_raw_part(*as_described_pair(estimated,source,target))
    assert result.status=='supported_input_comparison'
    assert any('Both source states are unspecified' in text for text in result.uncertainty)
    assert 'preparation_state' not in result.target_composition.properties


@pytest.mark.parametrize('source,target',[('rice','bulgur'),('chicken','pork'),('chickpea','lentil'),('egg','milk')])
def test_as_described_mode_does_not_infer_staple_or_protein_states(estimated,source,target):
    result=compare_raw_part(*as_described_pair(estimated,source,target))
    assert result.status=='needs_evidence'
    assert 'source.property:preparation_state' in result.missing_evidence


def test_as_described_dairy_does_not_hide_a_known_state_conflict(estimated):
    snapshot,edit,target=as_described_pair(estimated,'cream','yogurt')
    snapshot.ingredients[0].composition.properties['preparation_state']=PropertyAssertion.model_validate(fact('raw'))
    target.properties['preparation_state']=PropertyAssertion.model_validate(fact('cooked'))
    assert 'target.property_conflict:preparation_state' in compare_raw_part(snapshot,edit,target).blockers


def test_prototype_can_compare_pinned_tables_without_claiming_harmonized_methods(estimated):
    estimated[2].source_dataset='other released table'
    result=compare_raw_part(*estimated)
    assert result.status=='supported_input_comparison'
    assert any('across released food-composition tables' in text for text in result.uncertainty)
    assert estimated[0].ingredients[0].composition.source_dataset=='synthetic'


def test_verified_comparisons_still_require_same_source_table(inputs):
    inputs[2].source_dataset='other released table'
    assert 'cross_source_comparison_not_supported' in compare_raw_part(*inputs).blockers


def test_cross_table_estimates_keep_reporting_basis_checks(estimated):
    estimated[2].source_dataset='other released table'
    estimated[2].basis.amount_g=1
    assert 'target.unsupported_reporting_basis' in compare_raw_part(*estimated).blockers


def test_real_source_field_names_support_conditional_cross_table_estimates(estimated):
    from recipe_wrangler.tools.nutrition_completeness import EU
    snapshot, edit, target = estimated
    source = snapshot.ingredients[0].composition
    source.source_dataset, source.source_release = 'cofid', '2021'
    target.source_dataset, target.source_release = 'efsa_portugal', '2025'
    for composition in (source, target):
        composition.nutrient_definitions = {metric: f'{composition.source_dataset}:{composition.source_release}:source_field:{field}'
                                           for metric, field in EU.items() if metric != 'carbs_g'}
    result = compare_raw_part(snapshot, edit, target)
    assert result.status == 'supported_input_comparison'
    assert result.comparison_version == 'production_input_estimate_v4_source_fields'
    assert result.ingredient_delta['energy_kcal'] == -160.
    assert result.ingredient_delta['carbs_g'] is None
    assert result.nutrient_blockers['carbs_g'] == ['unknown_nutrient_definition']
    assert result.source_composition.nutrient_definitions == source.nutrient_definitions
    assert result.target_composition.nutrient_definitions == target.nutrient_definitions
    assert any('analytical methods are not fully harmonized' in s for s in result.uncertainty)
    snapshot.input_policy = 'verified'
    assert 'cross_source_comparison_not_supported' in compare_raw_part(snapshot, edit, target).blockers


@pytest.mark.parametrize('metric,left,right', [
    ('energy_kcal', 'source_field:unknown energy', 'source_field:unknown energy'),
    ('energy_kcal', 'source_field:Protein', 'source_field:Protein'),
    ('protein_g', 'source_field:Protein', 'source_field:unverified protein'),
    ('carbs_g', 'source_field:Carbohydrate, by difference', 'source_field:Carbohydrate, by difference'),
    ('carbs_g', 'native_component:CHO', 'native_component:CHO'),
])
def test_cross_table_normalization_does_not_invent_analytical_equivalence(estimated, metric, left, right):
    from recipe_wrangler.services.adaptation.comparison import _compatible_estimate_fields
    source, target = estimated[0].ingredients[0].composition, estimated[2]
    source.source_dataset, target.source_dataset = 'cofid', 'efsa_portugal'
    source.nutrient_definitions[metric] = f'cofid:{source.source_release}:{left}'
    target.nutrient_definitions[metric] = f'efsa_portugal:{target.source_release}:{right}'
    assert not _compatible_estimate_fields(source, target, metric)


def test_normalized_source_field_must_match_its_pinned_source(estimated):
    from recipe_wrangler.services.adaptation.comparison import _compatible_estimate_fields
    source, target = estimated[0].ingredients[0].composition, estimated[2]
    target.source_dataset = 'other'
    source.nutrient_definitions['energy_kcal'] = f'{source.source_dataset}:{source.source_release}:source_field:Energy, kcal'
    target.nutrient_definitions['energy_kcal'] = 'unrelated:table:source_field:Energy, kcal'
    assert not _compatible_estimate_fields(source, target, 'energy_kcal')


@pytest.mark.parametrize('metric,left,right', [
    ('energy_kcal','Energy, kcal','Energy (kcal) (kcal)'),
    ('protein_g','Protein','Protein (g)'),
    ('fat_g','Total lipid (fat)','Fat (g)'),
    ('sugar_g','Sugars, total','Total sugars (g)'),
    ('saturated_fat_g','Fatty acids, total saturated','Satd FA /100g fd (g)'),
    ('sodium_mg','Sodium, Na','Sodium (mg)'),
])
@pytest.mark.parametrize('reverse',[False,True])
def test_known_field_aliases_recover_only_conditional_comparisons(estimated, metric, left, right, reverse):
    snapshot,edit,target=estimated
    source=snapshot.ingredients[0].composition
    source.source_dataset,source.source_release='ciqual','frozen_legacy_inventory'
    target.source_dataset,target.source_release='irish','frozen_legacy_inventory'
    if reverse:left,right=right,left
    source.nutrient_definitions[metric]=f'{source.source_dataset}:{source.source_release}:source_field:{left}'
    target.nutrient_definitions[metric]=f'{target.source_dataset}:{target.source_release}:source_field:{right}'
    source.nutrient_definitions.pop('carbs_g',None)
    target.nutrient_definitions.pop('carbs_g',None)
    before=deepcopy((snapshot.model_dump(),target.model_dump()))
    result=compare_raw_part(snapshot,edit,target)
    assert result.status=='supported_input_comparison'
    assert result.comparison_version=='production_input_estimate_v5_field_aliases'
    expected=(target.values_per_100g[metric]-source.values_per_100g[metric])*200/100/snapshot.serves
    assert result.recipe_delta_per_serving[metric]==pytest.approx(expected)
    assert result.recipe_delta_per_serving['carbs_g'] is None
    assert result.source_composition.nutrient_definitions==source.nutrient_definitions
    assert result.target_composition.nutrient_definitions==target.nutrient_definitions
    assert (snapshot.model_dump(),target.model_dump())==before
    snapshot.input_policy='verified'
    assert 'cross_source_comparison_not_supported' in compare_raw_part(snapshot,edit,target).blockers


@pytest.mark.parametrize('metric,field', [
    ('energy_kcal','Energy (kJ) (kJ)'),('protein_g','Protein (mg)'),
    ('sodium_mg','Salt (g)'),('sugar_g','Added sugars (g)'),
    ('sugar_g','Sugar (g)'),('fibre_g','Fibre (Englyst) (g)'),
    ('fibre_g','Fiber (mg)'),
    ('carbs_g','Carbohydrate (g)'),
])
def test_alias_repair_does_not_guess_units_components_or_fibre_methods(estimated, metric, field):
    from recipe_wrangler.tools.nutrition_completeness import EU
    from recipe_wrangler.services.adaptation.comparison import _compatible_estimate_fields
    source,target=estimated[0].ingredients[0].composition,estimated[2]
    source.source_dataset,target.source_dataset='ciqual','irish'
    source.nutrient_definitions[metric]=f'{source.source_dataset}:{source.source_release}:source_field:{EU[metric]}'
    target.nutrient_definitions[metric]=f'{target.source_dataset}:{target.source_release}:source_field:{field}'
    assert not _compatible_estimate_fields(source,target,metric)


@pytest.mark.parametrize('source_field',['Fiber, total dietary','Fibre (g)','Fiber /100g fd (g)'])
@pytest.mark.parametrize('same_table',[False,True])
@pytest.mark.parametrize('values',[(0.,0.),(2.,1.2)])
def test_fibre_columns_share_fiber_g_name_without_rewriting_sources(estimated, source_field, same_table, values):
    snapshot,edit,target=estimated
    source=snapshot.ingredients[0].composition
    source.source_dataset,source.source_release='ciqual','frozen_legacy_inventory'
    target.source_dataset,target.source_release=('ciqual' if same_table else 'irish'),'frozen_legacy_inventory'
    source.nutrient_definitions['fibre_g']=f'{source.source_dataset}:{source.source_release}:source_field:{source_field}'
    target.nutrient_definitions['fibre_g']=f'{target.source_dataset}:{target.source_release}:source_field:Fiber (g)'
    source.values_per_100g['fibre_g'],target.values_per_100g['fibre_g']=values
    before=deepcopy((snapshot.model_dump(),target.model_dump()))
    result=compare_raw_part(snapshot,edit,target)
    assert result.status=='supported_input_comparison'
    assert result.comparison_version=='production_input_estimate_v6_fibre_fields'
    assert result.recipe_delta_per_serving['fibre_g']==pytest.approx(values[1]-values[0])
    assert 'fibre_g' not in result.nutrient_blockers
    assert any('Fiber (g)' in item for item in result.uncertainty)
    assert (snapshot.model_dump(),target.model_dump())==before
    snapshot.input_policy='verified'
    strict=compare_raw_part(snapshot,edit,target)
    assert strict.recipe_delta_per_serving.get('fibre_g') is None
