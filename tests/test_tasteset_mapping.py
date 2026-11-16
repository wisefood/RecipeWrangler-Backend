"""TASTEset field projection preserves representation gaps rather than scoring them away."""
from copy import deepcopy

import pytest

from test_contextual_parser import parser
from test_product_identity import annotator
from recipe_wrangler.matching.evaluation.tasteset_mapping import (
    LABELS, UNSUPPORTED, mapping_contract, normalize_label,
    project_facet_output, project_recipe_output,
)


def test_contract_covers_every_label_and_keeps_unsupported_classes():
    contract = mapping_contract()
    assert len(contract) == 15 and {r['label'] for r in contract} == set(LABELS)
    assert {r['label'] for r in contract if r['relationship'] == 'unsupported'} == UNSUPPORTED
    assert all(r['notes'] for r in contract)


@pytest.mark.parametrize('label,expected', [('physical quality', 'PHYSICAL_QUALITY'),
    ('TRADE NAME', 'TRADE_NAME'), ('POSSIBLE_SUBSTITUTE', 'POSSIBLE_SUBSTITUTE'), (' color ', 'COLOR')])
def test_label_spelling_normalization(label, expected):
    assert normalize_label(label) == expected


def test_unknown_label_is_not_silently_dropped():
    with pytest.raises(ValueError): normalize_label('BRAND')


def test_recipe_projection_retains_two_name_views_and_combined_measurement():
    parsed = {'ingredient_names': ['milk'], 'ingredient_match_names': ['skimmed milk'],
              'measurements': ['1/2 cup'], 'serves': 2}
    before = deepcopy(parsed)
    result = project_recipe_output(parsed)
    assert parsed == before
    assert [r['value'] for r in result['records']] == ['milk', 'skimmed milk', '1/2 cup']
    measurement = result['records'][-1]
    assert measurement['candidate_labels'] == ['QUANTITY', 'UNIT']
    assert measurement['representation'] == 'combined_measurement' and measurement['span'] is None
    split = result['measurement_splits'][0]
    assert split['quantity'] == 0.5 and split['unit'] == 'cup'
    assert split['raw_measurement'] == '1/2 cup' and split['position'] == 0
    assert not result['exact_span_predictions'] and not result['scoring_policy_finalized']


@pytest.mark.parametrize('parsed', [
    {'ingredient_names': ['milk'], 'measurements': []},
    {'ingredient_names': 'milk'}, {'measurements': [2]},
])
def test_invalid_recipe_vectors_reject(parsed):
    with pytest.raises(ValueError): project_recipe_output(parsed)


def test_real_context_parser_events_keep_valid_spans_and_source_fields(parser):
    annotation = parser.parse('large boiled eggs')
    before = deepcopy(annotation)
    result = project_facet_output(annotation)
    assert annotation == before
    assert any(r['candidate_labels'] == ['FOOD'] for r in result['records'])
    event = next(r for r in result['records'] if r['source_field'].startswith('modifiers') and r['value'] == 'boiled')
    assert event['candidate_labels'] == ['PROCESS'] and event['polarity'] == 'asserted'
    assert result['coordinate_text'][event['span'][0]:event['span'][1]] == event['quote']


@pytest.mark.parametrize('text,polarity', [('eggs, not boiled', 'negated'),
                                         ('eggs for frying', 'intended'),
                                         ('boiled or fried eggs', 'alternative')])
def test_modifier_polarity_does_not_become_food_exclusion_or_substitution(parser, text, polarity):
    result = project_facet_output(parser.parse(text))
    events = [r for r in result['records'] if r.get('polarity') == polarity]
    assert events
    assert all('EXCLUDED' not in r['candidate_labels'] and 'POSSIBLE_SUBSTITUTE' not in r['candidate_labels'] for r in events)


def test_excluded_food_head_is_a_partial_exclusion_candidate(parser):
    result = project_facet_output(parser.parse('without eggs'))
    food = next(r for r in result['records'] if r['source_field'] == 'foods[0]')
    assert food['candidate_labels'] == ['EXCLUDED'] and food['polarity'] == 'excluded'


def test_real_hybrid_diet_and_taste_ambiguity_is_retained(annotator):
    result = project_facet_output(annotator.annotate('milk, no added sugar'))
    record = next(r for r in result['records'] if r['source_field'] == 'facets.sugar_level')
    assert record['candidate_labels'] == ['DIET', 'TASTE'] and record['mapping_status'] == 'ambiguous'


def test_normalized_coordinates_are_not_claimed_to_be_original_input_offsets(annotator):
    annotation = annotator.annotate('  MILK, no added sugar  ')
    result = project_facet_output(annotation)
    assert result['coordinate_text'] != result['raw_input_text']
    assert result['coordinates_match_raw_input'] is False
    assert not result['exact_span_predictions']


def test_bad_supplied_span_is_never_repaired_by_substring_search():
    result = project_facet_output({'original_text': 'milk milk', 'foods': [
        {'text': 'milk', 'identity': 'milk', 'start': 1, 'end': 5, 'polarity': 'asserted'}]})
    assert result['records'][0]['span'] is None
    assert result['records'][0]['supplied_span_valid'] is False


def test_unknown_fields_false_values_and_optional_usage_remain_visible():
    result = project_facet_output({'original_text': 'milk', 'facets': {'gluten_free': False, 'future_field': 'x'},
        'context': {'optional': True, 'quantity_qualifier': 'to taste'}})
    rows = {r['source_field']: r for r in result['records']}
    assert rows['facets.gluten_free']['value'] is False
    assert rows['facets.future_field']['candidate_labels'] == []
    assert rows['context.optional']['candidate_labels'] == []
    assert rows['context.quantity_qualifier']['candidate_labels'] == []
    assert not any('EXCLUSIVE' in r['candidate_labels'] for r in result['records'])


def test_mapping_is_detached_and_requires_declared_coordinates():
    with pytest.raises(ValueError): project_facet_output({'facets': {'colour': 'white'}})
    original = {'original_text': 'milk', 'facets': {'processes': ['boiled']}}
    result = project_facet_output(original)
    result['records'][0]['value'].append('fried')
    assert original['facets']['processes'] == ['boiled']
