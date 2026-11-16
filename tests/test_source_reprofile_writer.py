"""Reprofile trust boundaries; transaction semantics have a session-local PG rehearsal."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from test_source_reprofile_package import case
from scripts.composition.prepare_source_reprofile import prepare
from scripts.composition import apply_source_reprofile as writer
from recipe_wrangler.repositories.matching_store import digest


@pytest.fixture
def package(case):
    return prepare(*case)


def test_reviewed_package_is_validated_without_mutation(package):
    before = deepcopy(package)
    assert len(writer.validate_package(package, digest(package))) == 4
    assert package == before


@pytest.mark.parametrize('field', ['candidate_profile_digest', 'input_hash', 'release_id',
                                   'rollback_target_run_id', 'replay_arguments', 'inputs'])
def test_rehashed_corrupt_package_rejects(package, field):
    entry = package['entries'][0]
    if field == 'replay_arguments': entry[field]['weights'] = [999]
    elif field == 'inputs':
        entry[field]['source_bindings'] = [{}]
        entry['input_hash'] = digest(entry['inputs'])
    else: entry[field] = 'other'
    with pytest.raises(ValueError): writer.validate_package(package, digest(package))


def test_unreviewed_package_never_enters_transaction(package):
    connection = Mock()
    with pytest.raises(ValueError, match='digest'):
        writer.apply_transaction(connection, package, operation_id='op', expected_package_digest='wrong',
                                 read_sources=Mock(), composition_digests=[], controlled_window=True)
    connection.execute.assert_not_called()


@pytest.mark.parametrize('action', ['apply', 'rollback'])
def test_missing_controlled_window_refuses_before_database(package, action):
    connection = Mock()
    kwargs = dict(operation_id='op', expected_package_digest=digest(package), read_sources=Mock())
    if action == 'apply': kwargs['composition_digests'] = []
    with pytest.raises(ValueError, match='Controlled'):
        getattr(writer, action + '_transaction')(connection, package, **kwargs)
    connection.execute.assert_not_called()


def test_explicit_transaction_required(package):
    connection = Mock(); connection.in_transaction.return_value = False
    with pytest.raises(ValueError, match='explicit transaction'): writer._start(connection, 'op', package)
    connection.execute.assert_not_called()


@pytest.mark.parametrize('operation_id', ['', ' ', None, 'x' * 201])
def test_invalid_operation_id(package, operation_id):
    connection = Mock(); connection.in_transaction.return_value = True
    with pytest.raises(ValueError, match='operation ID'): writer._start(connection, operation_id, package)
    connection.execute.assert_not_called()


@pytest.mark.parametrize('issue', ['missing', 'changed', 'revision'])
def test_fresh_source_guard_refuses_incomplete_or_changed_source(package, issue):
    entries = package['entries']; snapshot = {'recipe': {'title': 'Soup'}}
    sources = {'r': {'snapshot': snapshot, 'revision': 'rev'}}
    if issue == 'missing': sources.clear()
    elif issue == 'changed': sources['r']['snapshot'] = {}
    else: sources['r']['revision'] = None
    with pytest.raises(ValueError): writer._sources(entries, lambda ids: sources)


def test_duplicate_or_partial_regions_reject(package):
    package['entries'].pop()
    with pytest.raises(ValueError, match='four-region'): writer.validate_package(package, digest(package))


def test_saved_receipt_refuses_newer_mirror(monkeypatch):
    item = {'predecessor_run_id': 'old', 'new_run_id': 'new', 'applied_profile_digest': digest({}),
            'applied_mirror_digest': digest({'state': 'expected'})}
    monkeypatch.setattr(writer, '_current', lambda *a: ({'run_id': 'new', 'profile': {}}, {'state': 'changed'}))
    with pytest.raises(ValueError, match='advanced'): writer._verify_saved(Mock(), {'entries': [item]}, False)
