"""Prepare a read-only, replayable four-region reprofile package; never apply it."""
from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

from recipe_wrangler.matching.quantities import recipe_inputs, effective_quantity, QUANTITY_VERSION
from recipe_wrangler.matching.runtime import VERSION
from recipe_wrangler.repositories.matching_store import digest

REGIONS = {'eu', 'irish', 'hungarian', 'slovenian'}


def replay_arguments(profile: dict, *, require_complete_source_context: bool = True) -> dict:
    """Recover calculation inputs, including source identities, from durable details."""
    details = profile['details']
    if not details or [d['position'] for d in details] != list(range(len(details))):
        raise ValueError('Incomplete or unordered occurrence vector')
    if require_complete_source_context and any(not d.get('original_ingredient_text') or d.get('source_context_status') == 'requires_rematching'
           for d in details):
        raise ValueError('Incomplete source context')
    args = {'title': profile['title'], 'source': profile['source'], 'serves': profile['serves'],
            'ingredient_names': [d['ingredient'] for d in details],
            'weights': [d['original_weight_g'] for d in details],
            'original_texts': [d.get('original_ingredient_text') or '' for d in details],
            'quantity_evidence': [deepcopy(d['quantity_evidence']) for d in details]}
    bindings = [deepcopy(d.get('source_identity_evidence')) for d in details]
    if any(b is not None for b in bindings):
        args['source_bindings'] = bindings
        for d, binding in zip(details, bindings, strict=True):
            if binding is not None and (binding['release_id'] != profile['catalogue_release']
                    or str(binding['candidate_id']) != d['selected_candidate_id']
                    or binding['source_line'] != d['original_ingredient_text']
                    or binding['expected_label'] != d['matched_nutritional_ingredient']):
                raise ValueError('Source binding differs from selected occurrence')
    # Passing recipe_id would persist a run. Replays intentionally omit it.
    return args


def prepare(state: dict, candidates: list[dict]) -> dict:
    """Pin full replacements and predecessor rollback targets without relaxing repairs."""
    release = state['deployment']['release_id']
    if state['deployment']['mode'] != 'new' or state['release']['status'] != 'ready' or state['release']['release_id'] != release:
        raise ValueError('Deployment/release not ready')
    predecessors = {(p['recipe_id'], p['nutrition_source']): p for p in state['profiles']}
    if len(predecessors) != len(state['profiles']):
        raise ValueError('Duplicate predecessor')
    entries = []; seen = set(); vectors = {}
    for candidate in candidates:
        recipe_id = candidate['recipe_id'].rsplit('::', 1)[0]
        region = candidate['region']; key = (recipe_id, region)
        if region not in REGIONS or key in seen or key not in predecessors:
            raise ValueError('Duplicate, missing or unsupported predecessor')
        seen.add(key)
        old = predecessors[key]; profile = deepcopy(candidate['profile'])
        if old['release_id'] != release or profile['catalogue_release'] != release or profile['mechanism_version'] != VERSION:
            raise ValueError('Candidate release/runtime differs')
        if profile['source'] != region or profile['serves'] != old['profile']['serves']:
            raise ValueError('Candidate region or servings differ')
        if profile['title'] != state['recipes'][recipe_id]['recipe']['title']:
            raise ValueError('Recipe identity differs')
        args = replay_arguments(profile)
        inputs = recipe_inputs(args['ingredient_names'], args['weights'], args['original_texts'], serves=args['serves'])
        if inputs['quantity_input_hash'] != profile['quantity_input_hash']:
            raise ValueError('Candidate quantity hash differs from source vector')
        # Drained tins and whole pieces carry a recorded convention adjustment; compare against the same adjustment.
        expected = [effective_quantity(q, d.get('original_ingredient_text') or '')
                    for d, q in zip(profile['details'], inputs['quantities'], strict=True)]
        if any(d['weight_g'] != q['weight_g'] or d['quantity_evidence'] != q['quantity_evidence']
               or d.get('quantity_adjustment') != q.get('quantity_adjustment')
               or d['quantity_input_hash'] != inputs['quantity_input_hash']
               for d, q in zip(profile['details'], expected, strict=True)):
            raise ValueError('Candidate quantity evidence differs')
        if profile['quantity_version'] != QUANTITY_VERSION:
            raise ValueError('Candidate quantity version differs')
        if 'source_bindings' in args:
            inputs['source_bindings'] = deepcopy(args['source_bindings'])
        inputs['serves'] = args['serves']
        vectors.setdefault(recipe_id, []).append((region, profile['quantity_input_hash']))
        entries.append({'recipe_id': recipe_id, 'nutrition_source': region, 'release_id': release,
                        'predecessor_run_id': old['run_id'], 'predecessor_profile_digest': digest(old['profile']),
                        'source_snapshot_digest': digest(state['recipes'][recipe_id]),
                        'inputs': inputs, 'input_hash': digest(inputs), 'replay_arguments': args,
                        'candidate_profile_digest': digest(profile), 'profile': profile,
                        'rollback_target_run_id': old['run_id']})
    if not entries or seen != set(predecessors):
        raise ValueError('Candidates must cover the complete captured predecessor set')
    for group in vectors.values():
        if {r for r, h in group} != REGIONS or len({h for r, h in group}) != 1:
            raise ValueError('Four-region source quantity vectors differ')
    return {'version': 'source_reprofile_package_v1', 'status': 'prepared_not_activatable',
            'source_state_digest': digest(state), 'deployment': deepcopy(state['deployment']),
            'entries': entries, 'database_operations': [], 'production_writes': False,
            'remaining_gates': ['Fresh source/composition/predecessor guards at execution',
                                'Reviewed runtime artifact and controlled writer/worker window',
                                'Transactional writer, durable mirror backup and guarded rollback',
                                'One-recipe four-region propagation and rollback rehearsal',
                                'Measured budget before expanding scope']}


def build(directory: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError('Refuse to overwrite package')
    manifest = json.loads((directory / 'manifest.json').read_text())
    required = {'preview/final-source-capture.json'}
    for path in (directory / 'preview').glob('batch-*-capture.json.gz'):
        required.add(str(path.relative_to(directory)))
        required.add(str(path.with_name(path.name.replace('-capture.', '-offline.')).relative_to(directory)))
    if not required <= manifest['artifacts_sha256'].keys():
        raise ValueError('Required inputs are not sealed in evidence manifest')
    for name, expected in manifest['artifacts_sha256'].items():
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Input artifact changed: ' + name)
    runtime_path = 'src/recipe_wrangler/matching/runtime.py'
    runtime_file = Path(__file__).resolve().parents[2] / runtime_path
    if hashlib.sha256(runtime_file.read_bytes()).hexdigest() != manifest['code_and_docs_sha256'][runtime_path]:
        raise ValueError('Runtime differs from accepted evidence')
    preview = directory / 'preview'
    capture = json.loads((preview / 'final-source-capture.json').read_text())
    if digest(capture['state']) != capture['stable_digest']:
        raise ValueError('Source capture digest differs')
    candidates = []
    for path in sorted(preview.glob('batch-*-capture.json.gz')):
        with gzip.open(path, 'rt') as stream:
            live = json.load(stream)['results']
        with gzip.open(path.with_name(path.name.replace('-capture.', '-offline.')), 'rt') as stream:
            offline = json.load(stream)['results']
        if live != offline:
            raise ValueError('Offline replay differs')
        candidates.extend(live)
    package = prepare(capture['state'], candidates)
    package['evidence_manifest_sha256'] = hashlib.sha256((directory / 'manifest.json').read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        json.dump(package, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package = build(args.evidence, args.output)
    print(json.dumps({'status': package['status'], 'profiles': len(package['entries']), 'production_writes': False}))
