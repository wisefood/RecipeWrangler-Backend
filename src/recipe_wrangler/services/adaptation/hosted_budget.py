"""Durable, fail-closed transport for the explicitly approved prototype cases."""
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path

import openai

approved_case = ContextVar('approved_nutrition_case', default=None)
model_evidence = ContextVar('nutrition_model_evidence', default=None)
BASE_URL = 'https://openrouter.ai/api/v1'
MODEL = 'qwen/qwen3-32b'


class HostedBudgetError(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _record_evidence(kind: str, attempt: int, schema: str) -> None:
    evidence = model_evidence.get()
    if evidence is not None:
        evidence.append({'kind': kind, 'attempt': attempt, 'schema': schema})


def _cached_response(directory: Path, ledger: dict, messages: list, schema: str, max_tokens: int):
    """Reuse genuine completed responses only for the entire identical request."""
    for attempt in reversed(ledger['attempts']):
        if attempt['status'] != 'received' or attempt.get('schema') != schema:
            continue
        number = attempt['attempt']
        expected_tokens = attempt.get('max_tokens', 2048 if schema == 'RoleAssessment' else 4096)
        if expected_tokens != max_tokens:
            continue
        request_path = directory / f'hosted-request-{number}.json'
        response_path = directory / f'hosted-response-{number}.json'
        if not request_path.is_file() or not response_path.is_file():
            continue
        if json.loads(request_path.read_text()) != messages:
            continue
        response = json.loads(response_path.read_text())
        if response.get('model') != MODEL or not response.get('choices'):
            continue
        choice = response['choices'][0]
        if choice.get('finish_reason') == 'stop' and choice.get('message', {}).get('content'):
            _record_evidence('exact_request_cache', number, schema)
            return choice['message']['content']
    return None


def _load_approval(directory: Path) -> dict:
    approval = json.loads((directory / 'hosted-approval.json').read_text())
    attempts = approval.get('max_attempts')
    try:
        cost = Decimal(approval['max_cost_usd'])
    except (KeyError, TypeError, InvalidOperation):
        raise ValueError('Invalid hosted approval') from None
    if not (approval.get('approved') is True and type(attempts) is int and 1 <= attempts <= 24
            and isinstance(approval['max_cost_usd'], str)
            and cost.is_finite() and 0 < cost <= Decimal('0.50')
            and approval.get('cases') == 'cases.json'):
        raise ValueError('Invalid hosted approval')
    return approval


def approval_directory() -> Path:
    directory = Path(os.environ['ADAPT_HOSTED_APPROVAL_DIRECTORY']).resolve()
    _load_approval(directory)
    return directory


def approved_requests(directory: Path) -> dict:
    from .schemas import EnhancementRequest
    return {item['case']: (item['recipe_id'], EnhancementRequest(region=item.get('region', 'IE'), mode='nutrition',
        goal_nutrients=item['goal_nutrients'], constraints=item.get('constraints', {})))
        for item in json.loads((directory / 'cases.json').read_text())}


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def request(messages, config, *, max_tokens, schema):
    """Reserve every attempt, including failures; no retries or unscoped dispatch."""
    directory = approval_directory()
    approval = _load_approval(directory)
    case = approved_case.get()
    if case not in approved_requests(directory):
        raise HostedBudgetError('hosted_case_not_approved')
    if (config['source'], config['base_url'], config['model']) != ('openrouter', BASE_URL, MODEL):
        raise ValueError('Hosted destination differs from approval')
    if max_tokens not in (2048, 4096):
        raise ValueError('Unapproved token allowance')
    prompt_bytes = len(json.dumps(messages, ensure_ascii=False).encode())
    if prompt_bytes > 90000:
        raise ValueError('Hosted prompt exceeds approved limit')
    # Each UTF-8 byte is conservatively reserved as a token, plus framing.
    reservation = (Decimal(prompt_bytes + 4096) * Decimal('0.14')
                   + Decimal(max_tokens) * Decimal('0.57')) / Decimal(1000000)
    ledger_path = directory / 'hosted-ledger.json'
    with (directory / 'hosted-ledger.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {
            'max_attempts': approval['max_attempts'], 'max_cost_usd': approval['max_cost_usd'], 'attempts': []}
        if ledger['max_attempts'] != approval['max_attempts'] or ledger['max_cost_usd'] != approval['max_cost_usd']:
            raise ValueError('Changed durable budget')
        if os.getenv('ADAPT_HOSTED_RESPONSE_CACHE') == '1':
            cached = _cached_response(directory, ledger, messages, schema, max_tokens)
            if cached is not None:
                return cached
        if os.getenv('ADAPT_HOSTED_GENERATION_ENABLED', '1') != '1':
            raise HostedBudgetError('hosted_generation_disabled_cache_miss')
        attempts = ledger['attempts']
        reserved = sum((Decimal(item['reserved_usd']) for item in attempts), Decimal(0))
        if (len(attempts) >= approval['max_attempts'] or sum(item['case'] == case for item in attempts) >= 3
                or reserved + reservation > Decimal(approval['max_cost_usd'])):
            raise HostedBudgetError('hosted_budget_exhausted')
        number = len(attempts) + 1
        attempts.append({'attempt': number, 'case': case, 'schema': schema,
                         'reserved_usd': str(reservation), 'status': 'reserved', 'max_tokens': max_tokens})
        _write(ledger_path, ledger)
        _write(directory / f'hosted-request-{number}.json', messages)
    _record_evidence('fresh_hosted_attempt', number, schema)
    try:
        with openai.OpenAI(base_url=BASE_URL, api_key=config['api_key'],
                           timeout=min(config['timeout'], 90), max_retries=0) as client:
            response = client.chat.completions.create(model=MODEL, temperature=0.0,
                max_tokens=max_tokens, messages=messages, response_format={'type': 'json_object'},
                extra_body={'reasoning': {'enabled': False}, 'provider': {
                    'only': ['DeepInfra'], 'allow_fallbacks': False, 'require_parameters': True,
                    'max_price': {'prompt': 0.14, 'completion': 0.57}}})
        _write(directory / f'hosted-response-{number}.json', response.model_dump(mode='json'))
        status = 'received'
    except Exception as error:
        status = type(error).__name__
        raise
    finally:
        with (directory / 'hosted-ledger.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(ledger_path.read_text())
            ledger['attempts'][number - 1]['status'] = status
            if status == 'received' and response.usage is not None:
                ledger['attempts'][number - 1]['usage'] = response.usage.model_dump(mode='json')
            _write(ledger_path, ledger)
    if response.choices[0].finish_reason != 'stop':
        raise ValueError('incomplete_response')
    return response.choices[0].message.content or ''
