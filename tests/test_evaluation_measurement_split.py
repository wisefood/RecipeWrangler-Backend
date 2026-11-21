"""Evaluation value splitting must not apply production quantity-estimation heuristics."""
from copy import deepcopy

import pytest

from recipe_wrangler.matching.evaluation.measurement_split import split_measurement
from recipe_wrangler.matching.evaluation.tasteset_mapping import project_recipe_output, mapping_contract


@pytest.mark.parametrize('raw,quantity,unit', [
    ('1/2 cup', 0.5, 'cup'), ('200 g', 200, 'g'), ('200g', 200, 'g'),
    ('1.5 kilograms', 1.5, 'kg'), ('.5 L', 0.5, 'l'), ('1 1/2 cups', 1.5, 'cup'),
    ('1½ cups', 1.5, 'cup'), ('1 ½ cups', 1.5, 'cup'), ('½ cup', 0.5, 'cup'),
    ('⅓ cup', 1/3, 'cup'), ('1 / 2 cup', 0.5, 'cup'), ('3/2 cups', 1.5, 'cup'),
    ('2', 2, None), ('0', 0, None), ('0 g', 0, 'g'), ('2 cans', 2, 'can'),
    ('1 pinch', 1, 'pinch'), ('2 Tbsp.', 2, 'tbsp'), ('1 fl. oz.', 1, 'fl_oz'),
    ('1 fluid ounce', 1, 'fl_oz'), ('1 oz', 1, 'oz'), ('  ½   cups  ', 0.5, 'cup'),
])
def test_simple_values_and_explicit_aliases(raw, quantity, unit):
    result = split_measurement(raw)
    assert result['status'] == 'parsed'
    assert result['quantity'] == pytest.approx(quantity) and result['unit'] == unit
    assert result['raw_measurement'] == raw and result['span_scope'] == 'exported_measurement'
    a, b = result['quantity_span']; assert raw[a:b] == result['quantity_text']
    if result['unit_span'] is not None:
        a, b = result['unit_span']; assert raw[a:b] == result['unit_text']
    else: assert result['unit_text'] is None
    assert 'grams' not in result


@pytest.mark.parametrize('raw', ['1-2 cups', '1–2 tbsp', '1 to 2 cups',
    '2 (400 g) cans', '2 x 400 g', '1 400g can', '1 cup or 2 tbsp',
    '1 small', '2 eggs', 'half cup', 'a pinch', '11/2 cups', '12/3 cups',
    '1 3/2 cups', '1,5 l', '1 mysteryunit', '1e3 g', '1 cup, packed'])
def test_ambiguous_or_unsupported_values_remain_explicit(raw):
    result = split_measurement(raw)
    assert result['status'] == 'review_required' and result['reason']
    assert result['quantity'] is None and result['unit'] is None
    assert result['raw_measurement'] == raw
    assert result['quantity_span'] is None and result['unit_span'] is None


@pytest.mark.parametrize('raw', ['', '  ', '\n'])
def test_missing_is_not_zero_or_one(raw):
    result = split_measurement(raw)
    assert result['status'] == 'missing' and result['quantity'] is None and result['unit'] is None


@pytest.mark.parametrize('raw', ['to taste', 'AS NEEDED', 'as required', 'optional', 'for garnish'])
def test_unspecified_amount_is_not_a_count(raw):
    result = split_measurement(raw)
    assert result['status'] == 'unquantified' and result['quantity'] is None


@pytest.mark.parametrize('raw', ['-2 g', '1/0 cup', '11/0 cup', '1 1/0 cup', '9' * 400 + ' g'])
def test_invalid_numbers_fail_without_defaults(raw):
    result = split_measurement(raw)
    assert result['status'] == 'invalid' and result['quantity'] is None


@pytest.mark.parametrize('raw', [None, 1, {}, []])
def test_non_string_exports_reject(raw):
    with pytest.raises(ValueError): split_measurement(raw)


def test_adapter_preserves_aligned_occurrences_and_original_exports():
    parsed = {'ingredient_names': ['milk', 'beans', 'salt'],
              'ingredient_match_names': ['milk', 'beans', 'salt'],
              'measurements': ['1/2 cup', '2 (400 g) cans', '']}
    before = deepcopy(parsed); result = project_recipe_output(parsed)
    assert parsed == before
    assert [r['position'] for r in result['measurement_splits']] == [0, 1, 2]
    assert [r['status'] for r in result['measurement_splits']] == ['parsed', 'review_required', 'missing']
    assert len(result['records']) == 9
    assert not result['exact_span_predictions'] and not result['scoring_policy_finalized']


def test_contract_separates_derived_values_from_recipe_source_spans():
    contract = {r['label']: r for r in mapping_contract()}
    assert contract['QUANTITY']['comparison_fields'] == ['measurement_splits[].quantity']
    assert contract['UNIT']['comparison_fields'] == ['measurement_splits[].unit']
    assert contract['QUANTITY']['relationship'] == 'deterministic_value_projection'
