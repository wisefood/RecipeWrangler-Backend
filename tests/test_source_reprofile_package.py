"""Read-only reprofile packages preserve source identity and reject stale vectors."""
from copy import deepcopy

import pytest

from recipe_wrangler.matching.quantities import recipe_inputs, QUANTITY_VERSION
from recipe_wrangler.matching.runtime import VERSION
from scripts.composition.prepare_source_reprofile import prepare, replay_arguments


@pytest.fixture
def case():
    inputs = recipe_inputs(['Onion'], [15], ['15g Onion'])
    state = {'deployment': {'release_id': 'release', 'mode': 'new'},
             'release': {'release_id': 'release', 'status': 'ready'},
             'recipes': {'r': {'recipe': {'title': 'Soup'}}}, 'profiles': []}
    candidates = []
    for region in ('eu', 'irish', 'hungarian', 'slovenian'):
        detail = {'position': 0, 'ingredient': 'Onion', 'original_weight_g': 15,
                  'original_ingredient_text': '15g Onion', **inputs['quantities'][0],
                  'quantity_input_hash': inputs['quantity_input_hash'],
                  'selected_candidate_id': 'onion', 'matched_nutritional_ingredient': 'Onion'}
        if region == 'slovenian':
            detail['source_identity_evidence'] = {'release_id': 'release', 'candidate_id': 'onion',
                                                 'source_line': '15g Onion', 'expected_label': 'Onion'}
        profile = {'title': 'Soup', 'source': region, 'serves': 2, 'details': [detail],
                   'catalogue_release': 'release', 'mechanism_version': VERSION,
                   'quantity_input_hash': inputs['quantity_input_hash'], 'quantity_version': QUANTITY_VERSION}
        state['profiles'].append({'recipe_id': 'r', 'nutrition_source': region, 'run_id': 'old-' + region,
                                  'release_id': 'release', 'profile': {'serves': 2, 'details': []}})
        candidates.append({'recipe_id': 'r::' + region, 'region': region, 'profile': profile})
    return state, candidates


def test_full_replacement_preserves_binding_replay_and_rollback_without_mutation(case):
    before = deepcopy(case)
    package = prepare(*case)
    assert case == before
    assert package['production_writes'] is False and package['database_operations'] == []
    assert len(package['entries']) == 4
    bound = package['entries'][-1]
    assert bound['inputs']['source_bindings'] == bound['replay_arguments']['source_bindings']
    assert bound['rollback_target_run_id'] == 'old-slovenian'
    assert 'recipe_id' not in bound['replay_arguments']
    assert 'source_bindings' not in package['entries'][0]['inputs']
    assert len(bound['profile']['details']) != len(case[0]['profiles'][-1]['profile']['details'])


@pytest.mark.parametrize('issue', ['duplicate', 'missing', 'release', 'runtime', 'serves', 'title',
                                   'source', 'hash', 'weight', 'evidence', 'binding', 'context', 'position', 'deployment'])
def test_changed_or_incomplete_evidence_rejects(case, issue):
    state, candidates = case
    profile = candidates[-1]['profile']; detail = profile['details'][0]
    if issue == 'duplicate': candidates.append(deepcopy(candidates[-1]))
    elif issue == 'missing': candidates.pop()
    elif issue == 'release': profile['catalogue_release'] = 'other'
    elif issue == 'runtime': profile['mechanism_version'] = 'old'
    elif issue == 'serves': profile['serves'] = 4
    elif issue == 'title': profile['title'] = 'Other'
    elif issue == 'source': profile['source'] = 'irish'
    elif issue == 'hash': profile['quantity_input_hash'] = 'other'
    elif issue == 'weight': detail['weight_g'] = 900
    elif issue == 'evidence': detail['quantity_evidence']['source_line'] = '900g Onion'
    elif issue == 'binding': detail['source_identity_evidence']['candidate_id'] = 'other'
    elif issue == 'context': detail['original_ingredient_text'] = ''
    elif issue == 'position': detail['position'] = 2
    else: state['deployment']['mode'] = 'legacy'
    with pytest.raises(ValueError): prepare(state, candidates)


def test_replay_arguments_are_detached_from_durable_receipt(case):
    profile = case[1][-1]['profile']
    args = replay_arguments(profile)
    args['source_bindings'][0]['candidate_id'] = 'changed'
    assert profile['details'][0]['source_identity_evidence']['candidate_id'] == 'onion'


def test_build_rejects_changed_artifact_and_existing_output(tmp_path):
    import json
    from scripts.composition.prepare_source_reprofile import build
    (tmp_path / 'manifest.json').write_text(json.dumps({'artifacts_sha256': {
        'preview/final-source-capture.json': 'bad-hash'}}))
    (tmp_path / 'preview').mkdir()
    (tmp_path / 'preview/final-source-capture.json').write_text('{}')
    with pytest.raises(ValueError, match='artifact changed'):
        build(tmp_path, tmp_path / 'package.json')
    (tmp_path / 'package.json').write_text('preserve')
    with pytest.raises(ValueError, match='overwrite'):
        build(tmp_path, tmp_path / 'package.json')
    assert (tmp_path / 'package.json').read_text() == 'preserve'


def test_build_requires_sealed_capture(tmp_path):
    import json
    from scripts.composition.prepare_source_reprofile import build
    (tmp_path / 'manifest.json').write_text(json.dumps({'artifacts_sha256': {}}))
    with pytest.raises(ValueError, match='not sealed'):
        build(tmp_path, tmp_path / 'package.json')
