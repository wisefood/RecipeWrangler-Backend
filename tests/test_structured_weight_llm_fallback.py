"""Structured abstentions can use an explicit, traceable hosted-LLM fallback."""
from unittest.mock import Mock

import pytest

from recipe_wrangler.tools import ingredient_weight_tool as tool


@pytest.fixture
def llm(monkeypatch):
    client = Mock()
    client.invoke.return_value = 0.6
    monkeypatch.setattr(tool, 'ingredient_weight_llm_tool', client)
    monkeypatch.setattr(tool, 'LIVE_WEIGHT_LLM_ENABLED', True)
    monkeypatch.setattr(tool, 'WEIGHT_PORTION_TABLE_ONLY', True)
    monkeypatch.setattr(tool, '_reviewed_portion_table', lambda: {
        'rosemary': {'label': 'Rosemary', 'food_id': 'ausnut2023:fixture'}})
    monkeypatch.setattr(tool, 'canonical_name_to_usda', lambda *a: pytest.fail('unexpected resolver'))
    monkeypatch.setattr(tool, '_embedding_usda_link', lambda *a, **k: pytest.fail('unexpected embedding'))
    return client


def estimate(name='kaffir lime leaves', measurement='2 leaves', request=None, return_details=True):
    return tool.ingredient_weight_tool_usda.invoke({
        'ingredient_names': [name], 'measurements': [measurement],
        'portion_inputs': [request or {}], 'return_details': return_details})


def test_unlisted_name_uses_live_llm_and_retains_abstention_diagnostics(llm):
    result = estimate(request={'facets': {'preservation': 'dried'}})
    detail = result['details'][0]
    assert result['weights'] == [0.6]
    assert detail['weight_grams'] == 0.6 and detail['error'] is None
    assert detail['facet_matching']['blockers'] == ['not_in_reviewed_portion_table']
    assert detail['portion_match'] is None
    assert detail['pre_llm_error'] == 'Name is not in the reviewed portion table'
    assert detail['live_llm_fallback'] and detail['approximate']
    assert detail['source'] == 'LLM fallback'
    assert detail['match_type'] == 'live_llm_missing_portion_fallback'
    payload = llm.invoke.call_args.args[0]
    assert 'preservation: dried' in payload['ingredient']
    assert payload['parsed_quantity'] == '2' and payload['parsed_unit'] == 'leaf'


def test_reviewed_food_without_requested_portion_also_uses_llm(llm, monkeypatch):
    monkeypatch.setattr('recipe_wrangler.matching.portions.runtime.match_request',
                        lambda *a: {'status': 'unresolved', 'record': None, 'rejections': {'unit_mismatch': 1}})
    result = estimate('rosemary', '2 sprigs')
    assert result['weights'] == [0.6]
    assert result['details'][0]['facet_matching']['rejections'] == {'unit_mismatch': 1}
    llm.invoke.assert_called_once()


def test_successful_portion_and_direct_mass_do_not_call_llm(llm):
    assert estimate('banana', '2 medium', {'food_id': 'ingredient:banana'})['weights'] == [260]
    assert estimate('kaffir lime leaves', '5 g')['weights'] == [5]
    llm.invoke.assert_not_called()


def test_weight_list_return_also_applies_llm(llm):
    assert estimate(return_details=False) == [0.6]
    llm.invoke.assert_called_once()


def test_mixed_batch_keeps_weights_and_diagnostics_aligned(llm):
    result = tool.ingredient_weight_tool_usda.invoke({
        'ingredient_names': ['kaffir lime leaves', 'flour', 'rare fruit'],
        'measurements': ['2 leaves', '100 g', '2 pieces'],
        'portion_inputs': [{}, {}, {'match_mode': 'strict'}], 'return_details': True})
    assert result['weights'] == [0.6, 100, 0]
    assert result['details'][0]['live_llm_fallback']
    assert result['details'][1]['match_type'] == 'direct_mass'
    assert result['details'][2]['error']
    llm.invoke.assert_called_once()


def test_disabled_live_llm_keeps_abstention(llm, monkeypatch):
    monkeypatch.setattr(tool, 'LIVE_WEIGHT_LLM_ENABLED', False)
    result = estimate()
    assert result['weights'] == [0]
    assert result['details'][0]['error']
    llm.invoke.assert_not_called()


@pytest.mark.parametrize('name,measurement,portion_input', [
    ('kaffir lime leaves', '2 leaves', {'match_mode': 'strict'}),
    ('kaffir lime leaves', '', {}),
    ('kaffir lime leaves', '0 leaves', {}),
    ('kaffir lime leaves', '-1 leaves', {}),
    ('raw boiled kaffir lime leaves', '2 leaves', {}),
    ('dried kaffir lime leaves', '2 leaves', {'facets': {'preservation': 'fresh'}}),
    ('kaffir lime leaves', '2 small', {'facets': {'size': 'large'}}),
    ('banana', '2 medium', {'food_id': 'ingredient:potato'}),
])
def test_strict_invalid_or_conflicting_requests_do_not_call_llm(llm, name, measurement, portion_input):
    assert estimate(name, measurement, portion_input)['weights'] == [0]
    llm.invoke.assert_not_called()


@pytest.mark.parametrize('response', [float('nan'), float('inf'), 50.0, 0.0, -1.0, 'nonsense'])
def test_bad_llm_estimate_preserves_unresolved_result(llm, response):
    llm.invoke.return_value = response
    result = estimate()
    detail = result['details'][0]
    assert result['weights'] == [0]
    assert detail['weight_grams'] is None and detail['error']
    assert detail['live_llm_fallback'] is False and detail['live_llm_error']


def test_llm_failure_preserves_abstention(llm):
    llm.invoke.side_effect = TimeoutError('test timeout')
    result = estimate()
    assert result['weights'] == [0]
    assert 'test timeout' in result['details'][0]['live_llm_error']


def test_bare_count_checks_the_unit_actually_used_by_llm(llm):
    llm.invoke.return_value = 150.0
    result = estimate('rare fruit', '2')
    assert result['weights'] == [150]
    assert llm.invoke.call_args.args[0]['parsed_unit'] == 'piece'
    assert result['details'][0]['live_llm_unit'] == 'piece'
