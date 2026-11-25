"""Bounded read-only operator evidence never implies permission or writer control."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from test_source_reprofile_package import case
from scripts.composition.prepare_source_reprofile import prepare
from scripts.composition import preflight_source_reprofile as operator
from recipe_wrangler.repositories.matching_store import digest


@pytest.fixture
def setup(case):
    state, candidates = case
    for row in candidates: row['profile']['details'][0]['source_nutrition'] = row['region']
    package = prepare(state, candidates)
    compositions = {operator.COLLECTIONS[e['nutrition_source']]: {'candidate_id': 'onion', 'collection': operator.COLLECTIONS[e['nutrition_source']]}
                    for e in package['entries']}
    checks = [{'collection': c, 'candidate_id': 'onion', 'digest': digest(r)} for c, r in compositions.items()]
    sources = {'r': {'snapshot': state['recipes']['r'], 'revision': 'rev'}}
    connection = MagicMock(); calls = []
    def execute(statement, params=None):
        sql = str(statement); calls.append(sql); params = params or {}; result = MagicMock(); rows = []
        if 'FROM matching_deployment' in sql: rows = [state['deployment']]
        elif 'FROM matching_releases' in sql: rows = [{'status': 'ready'}]
        elif 'FROM matching_active_profiles' in sql:
            old = next(p for p in state['profiles'] if p['nutrition_source'] == params['source'])
            rows = [{'run_id': old['run_id'], 'release_id': old['release_id'], 'profile': old['profile']}]
        elif 'row_to_json(p)' in sql: rows = [{'mirror': {'source': params['source']}}]
        elif 'FROM matching_compositions' in sql: rows = [compositions[params['collection']]]
        elif 'information_schema.columns' in sql: rows = []
        elif 'FROM matching_projection_jobs' in sql: rows = []
        elif not sql.startswith('SET '): raise AssertionError(sql)
        result.mappings.return_value = rows
        return result
    connection.execute.side_effect = execute
    return package, sources, checks, connection, calls, state, compositions


def test_read_only_preflight_keeps_missing_schema_and_control_as_gates(setup):
    package, sources, checks, connection, calls, *_ = setup
    result = operator.inspect(connection, package, digest(package), lambda ids: deepcopy(sources), checks)
    assert result['profiles'] == 4 and len(result['mirror_digests']) == 4
    assert not result['ready_for_apply'] and not result['production_writes']
    assert not result['receipt_columns_present']
    assert len(result['remaining_gates']) == 4
    assert all(s.startswith(('SELECT ', 'SET ')) for s in calls)
    assert all('FOR UPDATE' not in s and 'advisory_lock' not in s for s in calls)


@pytest.mark.parametrize('issue', ['package', 'source', 'predecessor', 'composition', 'coverage', 'drift'])
def test_changed_evidence_rejects_before_emitting_preflight(setup, issue):
    package, sources, checks, connection, calls, state, compositions = setup
    reviewed = digest(package)
    if issue == 'package': reviewed = 'bad'
    elif issue == 'source': sources['r']['snapshot'] = {}
    elif issue == 'predecessor': state['profiles'][0]['run_id'] = 'later'
    elif issue == 'composition': next(iter(compositions.values()))['later'] = True
    elif issue == 'coverage': checks.pop()
    reader = MagicMock(side_effect=[deepcopy(sources), {}]) if issue == 'drift' else lambda ids: deepcopy(sources)
    with pytest.raises(ValueError): operator.inspect(connection, package, reviewed, reader, checks)


def driver_with(responses):
    driver = MagicMock(); session = driver.session.return_value.__enter__.return_value
    session.run.side_effect = responses
    return driver, session


def test_live_reader_uses_real_semantic_normalizer_and_bounded_queries():
    raw = {'recipe': {'recipe_id': 'r', 'title': 'Soup', 'serves': 2}, 'ingredients': [], 'original_ingredients': ['15g Onion']}
    driver, session = driver_with([[{'recipe_id': 'r', 'title': 'Soup'}], [], [], [], [raw]])
    result = operator.SourceReader(driver)(['r'])
    assert result['r']['revision'] == operator.recipe_revision(operator._record_to_recipe_dict(raw))
    assert len(session.run.call_args_list) == 5
    assert all(call.args[0].timeout == 60 for call in session.run.call_args_list)


@pytest.mark.parametrize('issue', ['duplicate', 'large_scope', 'duplicate_recipe', 'batch_limit'])
def test_reader_refuses_unbounded_or_ambiguous_sources(issue):
    responses = [[{'recipe_id': 'r'}], [], [], [], []]
    ids = ['r']
    if issue == 'duplicate': ids = ['r', 'r']
    elif issue == 'large_scope': ids = [str(i) for i in range(26)]
    elif issue == 'duplicate_recipe': responses[0] *= 2
    else: responses[1] = [{}] * 100
    driver, _ = driver_with(responses)
    with pytest.raises(ValueError): operator.SourceReader(driver)(ids)
