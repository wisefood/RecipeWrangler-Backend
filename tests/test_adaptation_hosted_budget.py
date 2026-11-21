import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from recipe_wrangler.services.adaptation import hosted_budget as budget


@pytest.fixture
def approved(tmp_path, monkeypatch):
    source = Path('analysis/nutrition-prototype-2026-11-10')
    if not (source / 'hosted-approval.json').exists():
        pytest.skip('local approval files in gitignored analysis/')
    for filename in ('cases.json', 'hosted-approval.json'):
        (tmp_path / filename).write_text((source / filename).read_text())
    monkeypatch.setenv('ADAPT_HOSTED_APPROVAL_DIRECTORY', str(tmp_path))
    return tmp_path


def test_transport_requires_case_and_pinned_destination_before_sdk(approved, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    config = dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL, api_key='secret', timeout=90)
    with pytest.raises(budget.HostedBudgetError, match='hosted_case_not_approved'):
        budget.request([], config, max_tokens=2048, schema='roles')
    token = budget.approved_case.set('chicken_default')
    try:
        with pytest.raises(ValueError, match='destination'):
            budget.request([], {**config, 'base_url': 'http://elsewhere'}, max_tokens=2048, schema='roles')
    finally:
        budget.approved_case.reset(token)
    client.assert_not_called()
    assert not (approved / 'hosted-ledger.json').exists()


def test_approved_case_uses_its_native_region_instead_of_always_irish(approved):
    (approved / 'cases.json').write_text(json.dumps([{'case':'slovenian', 'recipe_id':'recipe-si',
        'region':'SI', 'goal_nutrients':['saturated_fats']}]))
    assert budget.approved_requests(approved)['slovenian'][1].region == 'SI'


def test_a_smaller_explicit_budget_is_enforced_without_using_the_old_ceiling(approved, monkeypatch):
    path = approved / 'hosted-approval.json'
    approval = json.loads(path.read_text())
    approval.update(max_attempts=6, max_cost_usd='0.10')
    path.write_text(json.dumps(approval))
    (approved / 'hosted-ledger.json').write_text(json.dumps({'max_attempts':6, 'max_cost_usd':'0.10',
        'attempts':[{'case':str(i), 'reserved_usd':'0.001'} for i in range(6)]}))
    client = MagicMock()
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    token = budget.approved_case.set('pork')
    try:
        with pytest.raises(budget.HostedBudgetError, match='exhausted'):
            budget.request([], dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL),
                max_tokens=2048, schema='roles')
    finally:
        budget.approved_case.reset(token)
    client.assert_not_called()


@pytest.mark.parametrize('attempts,cost', [(True,'0.10'), (0,'0.10'), (25,'0.10'),
    (6,'0'), (6,'0.51'), (6,'NaN'), (6,'Infinity')])
def test_invalid_or_larger_budgets_are_not_authorized(approved, attempts, cost):
    path = approved / 'hosted-approval.json'
    approval = json.loads(path.read_text())
    approval.update(max_attempts=attempts, max_cost_usd=cost)
    path.write_text(json.dumps(approval))
    with pytest.raises(ValueError, match='Invalid hosted approval'):
        budget.approval_directory()


def test_failed_attempts_retain_reservations_and_stop_after_three(approved, monkeypatch):
    client = MagicMock(side_effect=ValueError('simulated transport failure'))
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    config = dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL, api_key='secret', timeout=90)
    token = budget.approved_case.set('pork')
    try:
        for _ in range(3):
            with pytest.raises(ValueError, match='simulated'):
                budget.request([{'role': 'user', 'content': 'test'}], config, max_tokens=2048, schema='roles')
        with pytest.raises(ValueError, match='exhausted'):
            budget.request([], config, max_tokens=2048, schema='roles')
    finally:
        budget.approved_case.reset(token)
    ledger = (approved / 'hosted-ledger.json').read_text()
    assert len(json.loads(ledger)['attempts']) == 3 and 'secret' not in ledger
    assert all(item['status'] == 'ValueError' for item in json.loads(ledger)['attempts'])
    assert client.call_count == 3


def test_aggregate_dollar_cap_prevents_dispatch(approved, monkeypatch):
    (approved / 'hosted-ledger.json').write_text(json.dumps({'max_attempts': 24, 'max_cost_usd': '0.50',
        'attempts': [{'case': 'pork', 'reserved_usd': '0.4999'}]}))
    client = MagicMock()
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    token = budget.approved_case.set('fish')
    try:
        with pytest.raises(ValueError, match='exhausted'):
            budget.request([], dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL),
                           max_tokens=4096, schema='roles')
    finally:
        budget.approved_case.reset(token)
    client.assert_not_called()


def test_success_pins_provider_disables_retries_and_records_usage(approved, monkeypatch):
    response = MagicMock()
    response.choices = [SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{}'))]
    response.model_dump.return_value = {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}
    response.usage.model_dump.return_value = {'prompt_tokens': 100, 'completion_tokens': 20}
    client = MagicMock()
    client.return_value.__enter__.return_value.chat.completions.create.return_value = response
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    token = budget.approved_case.set('dairy')
    try:
        assert budget.request([], dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL,
            api_key='secret', timeout=90), max_tokens=2048, schema='roles') == '{}'
    finally:
        budget.approved_case.reset(token)
    assert client.call_args.kwargs['max_retries'] == 0
    call = client.return_value.__enter__.return_value.chat.completions.create.call_args.kwargs
    assert call['extra_body']['provider']['only'] == ['DeepInfra']
    assert call['extra_body']['provider']['allow_fallbacks'] is False
    attempt = json.loads((approved / 'hosted-ledger.json').read_text())['attempts'][0]
    assert attempt['usage']['completion_tokens'] == 20 and attempt['status'] == 'received'


def test_prototype_rejects_other_recipes_and_routes_before_work(monkeypatch):
    # Import in an isolated environment; never invoke an allowed recipe here.
    if not Path('analysis/nutrition-prototype-2026-11-10/prototype_app.py').exists():
        pytest.skip('prototype lives in gitignored analysis/')
    with monkeypatch.context() as scoped:
        for key in ('ADAPT_HOSTED_APPROVAL_DIRECTORY', 'ADAPT_LLM_SOURCE', 'ADAPT_LLM_BASE_URL',
                    'ADAPT_LLM_MODEL', 'ADAPT_LLM_TIMEOUT', 'ELASTIC_URL'):
            scoped.setenv(key, '')
        spec = importlib.util.spec_from_file_location('approved_prototype',
            'analysis/nutrition-prototype-2026-11-10/prototype_app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = TestClient(module.app)
        assert client.get('/health').status_code == 200
        assert client.post('/api/v1/recipes/unapproved/adapt/enhance', json={'region': 'IE'}).status_code == 403
        assert client.post('/api/v1/recipes/0065010801/adapt/suggestions', json={'region': 'IE'}).status_code == 403
        assert client.post('/api/v1/recipes/0065010801/adapt/enhance',
                           json={'region': 'IE', 'goal_nutrients': ['sodium']}).status_code == 403


def test_exact_genuine_request_cache_avoids_dispatch_but_changed_context_misses(approved, monkeypatch):
    monkeypatch.setenv('ADAPT_HOSTED_RESPONSE_CACHE', '1')
    monkeypatch.setenv('ADAPT_HOSTED_GENERATION_ENABLED', '0')
    messages = [{'role': 'user', 'content': 'exact context and response schema'}]
    ledger = {'max_attempts': 24, 'max_cost_usd': '0.50', 'attempts': [
        {'attempt': 1, 'case': 'chicken_saturated_fat', 'schema': 'RoleAssessment',
         'reserved_usd': '0.01', 'status': 'received', 'max_tokens': 2048}]}
    (approved / 'hosted-ledger.json').write_text(json.dumps(ledger))
    (approved / 'hosted-request-1.json').write_text(json.dumps(messages))
    (approved / 'hosted-response-1.json').write_text(json.dumps({'model': budget.MODEL,
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}))
    client = MagicMock()
    monkeypatch.setattr(budget.openai, 'OpenAI', client)
    token = budget.approved_case.set('chicken_default')
    evidence_token = budget.model_evidence.set([])
    config = dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL)
    try:
        assert budget.request(messages, config, max_tokens=2048, schema='RoleAssessment') == '{}'
        assert budget.model_evidence.get() == [{'kind': 'exact_request_cache', 'attempt': 1, 'schema': 'RoleAssessment'}]
        with pytest.raises(budget.HostedBudgetError, match='cache_miss'):
            budget.request([{'role': 'user', 'content': 'changed recipe or schema'}], config,
                           max_tokens=2048, schema='RoleAssessment')
        with pytest.raises(budget.HostedBudgetError, match='cache_miss'):
            budget.request(messages, config, max_tokens=4096, schema='RoleAssessment')
    finally:
        budget.model_evidence.reset(evidence_token)
        budget.approved_case.reset(token)
    client.assert_not_called()
    assert json.loads((approved / 'hosted-ledger.json').read_text()) == ledger


@pytest.mark.parametrize('status,model', [('reserved', budget.MODEL), ('RateLimitError', budget.MODEL),
                                        ('received', 'different/model')])
def test_cache_never_uses_failed_unknown_or_other_model_responses(approved, monkeypatch, status, model):
    monkeypatch.setenv('ADAPT_HOSTED_RESPONSE_CACHE', '1')
    monkeypatch.setenv('ADAPT_HOSTED_GENERATION_ENABLED', '0')
    (approved / 'hosted-ledger.json').write_text(json.dumps({'max_attempts': 24, 'max_cost_usd': '0.50',
        'attempts': [{'attempt': 1, 'case': 'pork', 'schema': 'RoleAssessment', 'reserved_usd': '0.01', 'status': status}]}))
    (approved / 'hosted-request-1.json').write_text('[]')
    (approved / 'hosted-response-1.json').write_text(json.dumps({'model': model,
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}))
    token = budget.approved_case.set('pork')
    try:
        with pytest.raises(budget.HostedBudgetError, match='cache_miss'):
            budget.request([], dict(source='openrouter', base_url=budget.BASE_URL, model=budget.MODEL),
                           max_tokens=2048, schema='RoleAssessment')
    finally:
        budget.approved_case.reset(token)


def test_rate_limit_and_disabled_generation_are_distinct_from_invalid_assessments():
    import httpx
    import openai
    from recipe_wrangler.services.adaptation.llm_judge import llm_failure_reason
    error = openai.RateLimitError('provider private body', response=httpx.Response(429,
        request=httpx.Request('POST', budget.BASE_URL)), body=None)
    assert llm_failure_reason(error, 'invalid_assessment') == 'hosted_provider_rate_limited'
    assert llm_failure_reason(budget.HostedBudgetError('hosted_generation_disabled_cache_miss'),
                              'invalid_assessment') == 'hosted_generation_disabled_cache_miss'
    assert llm_failure_reason(ValueError('bad model JSON'), 'invalid_assessment') == 'invalid_assessment'
