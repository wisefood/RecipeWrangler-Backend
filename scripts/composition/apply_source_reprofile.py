"""Transactional reprofile primitives for a controlled operator; no standalone CLI.

Caller owns the transaction and must fence graph/profile writers and workers.
read_sources must freshly read snapshot/revision evidence on every invocation.
The receipt table migration is prepared separately and is never auto-installed.
"""
from collections.abc import Callable
from copy import deepcopy
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from recipe_wrangler.repositories import matching_store as store
from scripts.composition.activate_original_ingredient_context import FIELDS, _mirror
from scripts.composition.prepare_source_reprofile import REGIONS, replay_arguments
from recipe_wrangler.matching.runtime import VERSION, COLLECTIONS
from recipe_wrangler.matching.reviewed_aliases import reviewed_public_record


def validate_package(package: dict, expected_digest: str) -> list[dict]:
    if store.digest(package) != expected_digest or package.get('version') != 'source_reprofile_package_v1':
        raise ValueError('Reviewed package digest/version differs')
    entries = package['entries']; seen = set(); groups = {}
    if not entries or len(entries) > 100:
        raise ValueError('Scoped package requires 1–100 profiles')
    for entry in entries:
        key = (entry['recipe_id'], entry['nutrition_source']); profile = entry['profile']
        if key in seen or key[1] not in REGIONS:
            raise ValueError('Duplicate or unsupported profile')
        seen.add(key)
        if (store.digest(profile) != entry['candidate_profile_digest']
                or store.digest(entry['inputs']) != entry['input_hash']
                or profile['mechanism_version'] != VERSION
                or profile['catalogue_release'] != entry['release_id']
                or profile['source'] != key[1]
                or replay_arguments(profile) != entry['replay_arguments']
                or entry['rollback_target_run_id'] != entry['predecessor_run_id']):
            raise ValueError('Candidate inputs/profile/rollback evidence differs')
        args = entry['replay_arguments']; inputs = entry['inputs']
        if (inputs['names'] != args['ingredient_names'] or inputs['weights'] != args['weights']
                or inputs['originals'] != args['original_texts'] or inputs['serves'] != args['serves']
                or inputs.get('source_bindings') != args.get('source_bindings')
                or inputs['quantity_input_hash'] != profile['quantity_input_hash']):
            raise ValueError('Durable inputs differ from replay')
        groups.setdefault(key[0], []).append((key[1], profile['quantity_input_hash'], entry['source_snapshot_digest']))
    for group in groups.values():
        if {r for r, q, s in group} != REGIONS or len({(q, s) for r, q, s in group}) != 1:
            raise ValueError('Complete aligned four-region profiles required')
    return sorted(entries, key=lambda e: (e['recipe_id'], e['nutrition_source']))


def _start(connection, operation_id, package):
    if not connection.in_transaction():
        raise ValueError('Caller must own an explicit transaction')
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
        raise ValueError('Invalid operation ID')
    connection.execute(text("SET LOCAL statement_timeout='60s'"))
    connection.execute(text("SET LOCAL lock_timeout='5s'"))
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('matching_production_promotion',0))"))
    deployment = dict(connection.execute(text('SELECT release_id,mode FROM matching_deployment WHERE singleton FOR UPDATE')).mappings().one())
    if deployment != package['deployment'] or deployment['mode'] != 'new':
        raise ValueError('Deployment changed')
    release = connection.execute(text('SELECT status FROM matching_releases WHERE release_id=:release FOR SHARE'),
                                 {'release': deployment['release_id']}).scalar()
    if release != 'ready' or any(e['release_id'] != deployment['release_id'] for e in package['entries']):
        raise ValueError('Release not ready or changed')
    receipt = connection.execute(text('SELECT * FROM matching_reprofile_receipts WHERE operation_id=:id FOR UPDATE'),
                                 {'id': operation_id}).mappings().first()
    if receipt and (receipt['package_digest'] != store.digest(package)
                    or store.digest(receipt['payload']) != receipt['payload_digest']):
        raise ValueError('Operation reused or receipt corrupted')
    return dict(receipt) if receipt else None


def _sources(entries, read_sources):
    ids = sorted({e['recipe_id'] for e in entries})
    sources = read_sources(ids)
    if set(sources) != set(ids):
        raise ValueError('Incomplete fresh source guard')
    for entry in entries:
        source = sources[entry['recipe_id']]
        if store.digest(source['snapshot']) != entry['source_snapshot_digest'] or not source.get('revision'):
            raise ValueError('Source recipe changed')
    return sources


def _current(connection, entry):
    params = {'recipe': entry['recipe_id'], 'source': entry['nutrition_source']}
    row = connection.execute(text('''SELECT a.run_id,r.profile,r.release_id FROM matching_active_profiles a
        JOIN matching_profile_runs r ON r.run_id=a.run_id AND r.recipe_id=a.recipe_id AND r.nutrition_source=a.nutrition_source
        WHERE a.recipe_id=:recipe AND a.nutrition_source=:source FOR UPDATE OF a,r'''), params).mappings().one()
    mirror = connection.execute(text('''SELECT row_to_json(p) FROM "nutrients-recipe-profiles" p
        WHERE recipe_id=:recipe AND nutrition_source=:source FOR UPDATE'''), params).scalar()
    if mirror is None:
        raise ValueError('Production mirror missing')
    return dict(row), mirror


def _compositions(connection, entries, expected):
    selected = {(COLLECTIONS[d['source_nutrition']], d['selected_candidate_id'])
                for e in entries for d in e['profile']['details'] if d.get('selected_candidate_id')}
    supplied = {(r['collection'], r['candidate_id']): r['digest'] for r in expected}
    if len(supplied) != len(expected) or set(supplied) != selected:
        raise ValueError('Composition guard coverage differs')
    release = entries[0]['release_id']
    for (collection, candidate), digest in sorted(supplied.items()):
        reviewed = reviewed_public_record(candidate)
        if reviewed is not None:  # reviewed public value outside the release: guard the reviewed row itself
            if store.digest(reviewed) != digest:
                raise ValueError('Selected composition changed')
            continue
        row = connection.execute(text('''SELECT * FROM matching_compositions
            WHERE release_id=:release AND collection=:collection AND candidate_id=:candidate FOR SHARE'''),
            {'release': release, 'collection': collection, 'candidate': candidate}).mappings().one()
        if store.digest(dict(row)) != digest:
            raise ValueError('Selected composition changed')


def _queue(connection, entries):
    generations = {}
    for recipe_id in sorted({e['recipe_id'] for e in entries}):
        store.enqueue_projection(connection, recipe_id)
        generations[recipe_id] = connection.execute(text('SELECT generation FROM matching_projection_jobs WHERE recipe_id=:id'),
                                                   {'id': recipe_id}).scalar_one()
    return generations


def _save(connection, operation_id, package, status, payload):
    connection.execute(text('''INSERT INTO matching_reprofile_receipts(operation_id,package_digest,status,payload,payload_digest)
        VALUES(:id,:package,:status,CAST(:payload AS jsonb),:digest)
        ON CONFLICT(operation_id) DO UPDATE SET status=EXCLUDED.status,payload=EXCLUDED.payload,
        payload_digest=EXCLUDED.payload_digest,updated_at=now()'''),
        {'id': operation_id, 'package': store.digest(package), 'status': status,
         'payload': json.dumps(payload, allow_nan=False), 'digest': store.digest(payload)})


def _verify_saved(connection, payload, rolled_back):
    for item in payload['entries']:
        current, mirror = _current(connection, item)
        run = item['predecessor_run_id'] if rolled_back else item['new_run_id']
        profile_digest = item['predecessor_profile_digest'] if rolled_back else item['applied_profile_digest']
        mirror_digest = store.digest(item['previous_mirror']) if rolled_back else item['applied_mirror_digest']
        if current['run_id'] != run or store.digest(current['profile']) != profile_digest or store.digest(mirror) != mirror_digest:
            raise ValueError('Profile or mirror advanced since operation')


def apply_transaction(connection: Connection, package: dict, *, operation_id: str, expected_package_digest: str,
                      read_sources: Callable[[list[str]], dict], composition_digests: list[dict],
                      mirror_digests: list[dict] | None = None, controlled_window: bool = False) -> dict:
    """Apply all regions atomically; caller must commit before treating receipt as durable."""
    if not controlled_window:
        raise ValueError('Controlled graph/profile writer and worker window required')
    entries = validate_package(package, expected_package_digest)
    if not connection.in_transaction():
        raise ValueError('Caller must own an explicit transaction')
    with connection.begin_nested():
        prior = _start(connection, operation_id, package)
        sources = _sources(entries, read_sources)
        _compositions(connection, entries, composition_digests)
        if prior:
            if prior['status'] != 'applied':
                raise ValueError('Operation was rolled back; use a newly reviewed operation')
            if {r: source['revision'] for r, source in sources.items()} != prior['payload']['source_revisions']:
                raise ValueError('Source revision changed')
            _verify_saved(connection, prior['payload'], False)
            return deepcopy(prior['payload'])
        guards = {(r['recipe_id'], r['nutrition_source']): r['digest'] for r in (mirror_digests or [])}
        if len(guards) != len(mirror_digests or []) or set(guards) != {(e['recipe_id'], e['nutrition_source']) for e in entries}:
            raise ValueError('Reviewed mirror guard coverage differs')
        payload = {'operation_id': operation_id, 'entries': [], 'source_revisions': {r: s['revision'] for r, s in sources.items()}}
        # Verify and lock every predecessor and mirror before changing any profile.
        for entry in entries:
            current, mirror = _current(connection, entry)
            if (current['run_id'] != entry['predecessor_run_id'] or current['release_id'] != entry['release_id']
                    or store.digest(current['profile']) != entry['predecessor_profile_digest']
                    or current['profile'].get('serves') != entry['profile']['serves']):
                raise ValueError('Active predecessor changed')
            if store.digest(mirror) != guards[entry['recipe_id'], entry['nutrition_source']]:
                raise ValueError('Production mirror changed since preflight')
            profile = deepcopy(entry['profile'])
            profile.update(source_recipe_revision=sources[entry['recipe_id']]['revision'],
                           source_reprofile_operation_id=operation_id,
                           source_reprofile_of_run_id=entry['predecessor_run_id'])
            payload['entries'].append({**deepcopy(entry), 'applied_profile': profile,
                                      'applied_profile_digest': store.digest(profile), 'previous_mirror': mirror})
        for item in payload['entries']:
            run_id = store.insert_run(connection, item['recipe_id'], item['nutrition_source'], item['release_id'],
                                      VERSION, item['inputs'], item['applied_profile'], activate=True)
            item['new_run_id'] = run_id
            _mirror(connection, item['recipe_id'], item['nutrition_source'], run_id)
            current, mirror = _current(connection, item)
            if store.digest(current['profile']) != item['applied_profile_digest']:
                raise ValueError('Saved immutable profile differs')
            item['applied_mirror_digest'] = store.digest(mirror)
        if _sources(entries, read_sources) != sources:
            raise ValueError('Source changed during transaction')
        payload['forward_generations'] = _queue(connection, entries)
        _save(connection, operation_id, package, 'applied', payload)
        return deepcopy(payload)


def rollback_transaction(connection: Connection, package: dict, *, operation_id: str, expected_package_digest: str,
                         read_sources: Callable[[list[str]], dict], controlled_window: bool = False) -> dict:
    """Restore complete touched mirror fields and pointers, refusing intervening edits."""
    if not controlled_window:
        raise ValueError('Controlled graph/profile writer and worker window required')
    entries = validate_package(package, expected_package_digest)
    if not connection.in_transaction():
        raise ValueError('Caller must own an explicit transaction')
    with connection.begin_nested():
        receipt = _start(connection, operation_id, package)
        if receipt is None:
            raise ValueError('No committed operation receipt')
        payload = deepcopy(receipt['payload'])
        sources = _sources(entries, read_sources)
        if {r: s['revision'] for r, s in sources.items()} != payload['source_revisions']:
            raise ValueError('Source revision changed')
        _verify_saved(connection, payload, receipt['status'] == 'rolled_back')
        if receipt['status'] == 'rolled_back':
            return payload
        for item in payload['entries']:
            params = {'recipe': item['recipe_id'], 'source': item['nutrition_source']}
            connection.execute(text('''UPDATE matching_active_profiles SET run_id=:old
                WHERE recipe_id=:recipe AND nutrition_source=:source'''), {**params, 'old': item['predecessor_run_id']})
            assignments = ','.join('"' + field + '"=old."' + field + '"' for field in FIELDS)
            changed = connection.execute(text('UPDATE "nutrients-recipe-profiles" p SET ' + assignments + '''
                FROM json_populate_record(NULL::"nutrients-recipe-profiles",CAST(:backup AS json)) old
                WHERE p.recipe_id=:recipe AND p.nutrition_source=:source'''),
                {**params, 'backup': json.dumps(item['previous_mirror'], allow_nan=False)}).rowcount
            if changed != 1:
                raise ValueError('Production mirror disappeared')
        if _sources(entries, read_sources) != sources:
            raise ValueError('Source changed during rollback')
        _verify_saved(connection, payload, True)
        payload['rollback_generations'] = _queue(connection, entries)
        _save(connection, operation_id, package, 'rolled_back', payload)
        return deepcopy(payload)
