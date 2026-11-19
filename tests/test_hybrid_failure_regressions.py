"""Regressions from the Irish in-house pilot, including adverse contrasts."""
from pathlib import Path

import pytest

from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator, HybridMatcher, assess_compatibility
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def matcher():
    specs = [('milk', 'milk'), ('bread', 'cereal'), ('yogurt', 'generic'),
             ('strawberry', 'generic'), ('zucchini', 'generic'), ('fat', 'generic'),
             ('spread', 'generic'), ('custard', 'generic'), ('icing', 'generic'), ('sugar', 'generic'), ('cinnamon', 'generic'), ('powder', 'generic'), ('pasta', 'pasta'), ('penne', 'pasta'), ('fusilli', 'pasta'), ('macaroni', 'pasta'), ('chicken breast', 'meat'), ('chicken', 'meat'),
             ('grilled chicken breast', 'meat'), ('minced beef', 'meat')]
    terms = [{'term_id': str(i), 'preferred_label': name, 'definition': '', 'curation_status': '',
              'labels': [{'text': name, 'type': 'preferred'}]} for i, (name, _) in enumerate(specs)]
    parser = ContextualParser(IngredientDictionary({'schema_version': 1, 'provenance': {}, 'terms': terms}),
                              {str(i): {family} for i, (_, family) in enumerate(specs)})
    return HybridMatcher(HybridAnnotator(parser))


def source_match(matcher, query, label, *, native=None, issues=None):
    a = matcher.annotate(label)
    matcher.composition_annotations = {'source': {'label': label, 'text_annotation': a,
        'facets': dict(a['facets'], **(native or {})), 'native_facets': native or {},
        'issues': issues or [], 'native_evidence': [], 'metadata': {}}}
    return matcher.match(query, [{'key': label, 'label': label, 'records': [{'catalogue_key': 'source'}]}])


@pytest.mark.parametrize('query,label,identity', [
    ('yoghurt', 'yogurt', 'yogurt'), ('strawberries', 'strawberry', 'strawberry'),
    ('courgettes', 'zucchini', 'zucchini'), ('wholegrain bread', 'bread, wholegrain', 'bread'),
])
def test_common_spelling_plural_and_order(matcher, query, label, identity):
    assert matcher.annotate(query)['main_ingredient'] == identity
    assert source_match(matcher, query, label)['status'] == 'proposal'


def test_process_prefix_does_not_steal_identity_or_lose_process(matcher):
    a = matcher.annotate('grilled chicken breast')
    assert a['main_ingredient'] == 'chicken'
    assert a['facets']['processes'] == ['grilled']
    assert a['facets']['food_part'] == 'breast'
    assert matcher.annotate('minced beef')['facets']['processes'] == ['minced']


def test_breast_fillet_does_not_create_contradictory_parts(matcher):
    a = matcher.annotate('chicken breast fillet')
    assert a['facets']['food_part'] == 'breast'
    assert 'conflicting_assertions:food_part' not in a['review_reasons']
    assert a['unresolved'] == []


@pytest.mark.parametrize('name,field,value', [
    ('low-fat spread', 'fat_level', 'low'), ('reduced fat spread', 'fat_level', 'reduced'),
    ('low salt spread', 'sodium_level', 'low'), ('reduced-sodium spread', 'sodium_level', 'reduced'),
])
def test_nutrition_qualifiers_are_not_milk_specific(matcher, name, field, value):
    a = matcher.annotate(name)
    assert a['main_ingredient'] == 'spread'
    assert a['facets'][field] == value
    assert a['unresolved'] == []


def test_complete_catalogue_name_supplies_identity_when_ontology_is_missing(matcher):
    assert matcher.annotate('mixed herbs')['main_ingredient'] is None
    result = source_match(matcher, 'mixed herbs', 'Mixed herbs')
    assert result['status'] == 'proposal'
    assert result['match_method'] == 'catalogue_name'
    assert result['proposed_record_keys'] == ['source']
    # Evidence is source wording, not an invented ontology entry or extracted facet.
    assert result['annotation']['main_ingredient'] is None
    assert result['annotation']['facets']['preservation_methods'] == []


def test_complete_name_rule_needs_a_real_source_not_a_vector_hit(matcher):
    result = matcher.match('mixed herbs', [{'key': 'same', 'label': 'mixed herbs', 'retrieval_reason': 'vector'}])
    assert result['status'] != 'proposal'


@pytest.mark.parametrize('query,label', [
    ('mixed herbs', 'Mixed herbs, dried'), ('Cajun spice mix', 'Chinese 5 spice'),
    ('low-fat milk', 'milk, 0% fat'), ('milk, 1.5% fat', 'milk, 15% fat'),
    ('milk not boiled', 'milk, boiled'), ('milk for boiling', 'milk, boiled'),
    ('milk with bread', 'bread with milk'), ('milk, no added sugar', 'milk, sugar free'),
])
def test_more_coverage_cannot_hide_material_differences(matcher, query, label):
    assert source_match(matcher, query, label)['status'] != 'proposal'


def test_exact_missing_ontology_still_preserves_native_issues(matcher):
    assert source_match(matcher, 'mixed herbs', 'Mixed herbs', issues=['conflicting_source_code'])['status'] != 'proposal'


def test_exact_known_qualifier_cannot_override_conflicting_native_value(matcher):
    result = source_match(matcher, 'low-fat spread', 'low-fat spread', native={'fat_level': 'whole'})
    assert result['status'] != 'proposal'


def test_unknown_herb_name_can_move_a_complete_preservation_word(matcher):
    result = source_match(matcher, 'dried mixed herbs', 'Mixed herbs, dried')
    assert result['status'] == 'proposal'
    assert result['match_method'] == 'catalogue_name'


def test_unknown_food_words_cannot_be_reordered(matcher):
    assert source_match(matcher, 'almond chocolate', 'chocolate almond')['status'] != 'proposal'


@pytest.mark.parametrize('query,label', [
    ('vegetable stock cube', 'Stock cubes, vegetable'),
    ('chicken stock cube', 'Stock cubes, chicken'),
    ('low-salt vegetable stock cube', 'Stock cube, vegetable, low salt'),
])
def test_stock_cube_preserves_product_source_and_concentration(matcher, query, label):
    q = matcher.annotate(query)
    c = matcher.annotate(label)
    assert q['main_ingredient'] == c['main_ingredient']
    assert q['facets']['physical_form'] == c['facets']['physical_form'] == 'cube'
    assert source_match(matcher, query, label)['status'] == 'proposal'


def test_stock_cannot_become_meat_or_ready_to_drink_at_cube_weight(matcher):
    assert source_match(matcher, 'chicken stock cube', 'chicken breast')['status'] != 'proposal'
    assert source_match(matcher, 'vegetable stock cube', 'Vegetable stock RTE')['status'] != 'proposal'


def test_stock_product_grammar_does_not_steal_another_food_head(matcher):
    a = matcher.annotate('chicken breast with vegetable stock')
    assert a['main_ingredient'] is None
    assert len(a['foods']) == 2
    assert a['foods'][0]['identity'] == 'chicken'


def test_cultured_milk_is_ranked_as_an_extra_product_style(matcher):
    result = matcher.match('low-fat milk', [
        {'key': 'cultured', 'label': 'Milk, cultured, low fat'},
        {'key': 'fortified', 'label': 'Milk, low fat, vitamin D fortified'},
    ])
    assert result['candidates'][0]['key'] == 'fortified'
    cultured = next(c for c in result['candidates'] if c['key'] == 'cultured')
    assert cultured['annotation']['qualifiers']['product_style'] == 'cultured'


def test_milk_component_of_an_unknown_dish_cannot_be_the_primary_milk_food(matcher):
    a = matcher.annotate('BLIN WITH LOW-FAT MILK')
    assert a['main_ingredient'] is None
    assert 'only_component_food_head_identified' in a['review_reasons']
    result = matcher.match('low-fat milk', [
        {'key': 'blin', 'label': 'BLIN WITH LOW-FAT MILK'},
        {'key': 'fortified', 'label': 'Milk, low fat, vitamin D fortified'},
    ])
    assert result['candidates'][0]['key'] == 'fortified'


def test_reordered_unknown_name_does_not_hide_conflicting_native_variants(matcher):
    label = 'Mixed herbs, dried'
    a = matcher.annotate(label)
    matcher.composition_annotations = {state: {'label': label, 'text_annotation': a,
        'facets': dict(a['facets'], preparation_state=state),
        'native_facets': {'preparation_state': state}, 'issues': [],
        'native_evidence': [], 'metadata': {}} for state in ['raw', 'cooked']}
    result = matcher.match('dried mixed herbs', [{'key': 'herbs', 'label': label,
        'records': [{'catalogue_key': state} for state in ['raw', 'cooked']]}])
    assert result['status'] != 'proposal'


def test_unlisted_noun_compound_preserves_primary_product(matcher):
    assert matcher.annotate('milk custard')['main_ingredient'] == 'custard'
    assert matcher.annotate('icing sugar')['main_ingredient'] == 'sugar'
    assert source_match(matcher, 'low-fat milk', 'Milk custard, low fat')['status'] != 'proposal'
    result = source_match(matcher, 'icing sugar', 'Sugar, icing')
    assert result['status'] == 'proposal'


def test_parenthetical_source_label_is_not_flattened_into_an_ingredient_name(matcher):
    result = source_match(matcher, 'icing sugar', 'Icing (sugar)')
    assert result['status'] != 'proposal'


def test_physical_form_word_cannot_replace_a_food_head(matcher):
    a = matcher.annotate('cinnamon powder')
    assert a['main_ingredient'] == 'cinnamon'
    assert a['facets']['physical_form'] == 'powder'
    assert source_match(matcher, 'cinnamon powder', 'Cinnamon, powder')['status'] == 'proposal'


def test_pasta_shapes_are_retained_without_claiming_different_nutrition(matcher):
    query = matcher.annotate('penne pasta')
    candidate = matcher.annotate('macaroni pasta')
    assert query['qualifiers']['pasta_shape'] == 'penne'
    assert candidate['qualifiers']['pasta_shape'] == 'macaroni'
    assert assess_compatibility(query, candidate).relation == 'compatible_proposal'
    assert matcher.annotate('pasta')['qualifiers'].get('pasta_shape') is None
    assert source_match(matcher, 'penne pasta, raw', 'Macaroni, boiled')['status'] != 'proposal'




def test_generic_meat_part_on_an_exact_catalogue_label_does_not_contradict_the_cut(matcher):
    import copy
    query, candidate = copy.deepcopy(matcher.annotate('chicken breast')), copy.deepcopy(matcher.annotate('chicken breast'))
    query['facets']['food_part'], candidate['facets']['food_part'] = 'breast', 'meat'
    assert 'property.food_part' in [r['rule_id'] for r in assess_compatibility(query, candidate).rules if r['relation'] == 'reject']
    assert 'property.food_part' not in [r['rule_id'] for r in assess_compatibility(query, candidate, exact_description=True).rules if r['relation'] == 'reject']
