"""Read-only operator preflight for reviewed four-region source reprofiles."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from neo4j import READ_ACCESS, Query
from sqlalchemy import text

from recipe_wrangler.matching.source_context import recipe_revision
from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.tools.fetch_recipe_info import _RECIPE_INFO_QUERY, _record_to_recipe_dict
from scripts.composition.apply_source_reprofile import validate_package, _sources, COLLECTIONS
from recipe_wrangler.matching.reviewed_aliases import reviewed_public_record


class SourceReader:
    """Use the archived snapshot shape and the application's semantic revision."""
    def __init__(self, driver):
        self.driver = driver

    def __call__(self, recipe_ids: list[str]) -> dict:
        if not recipe_ids or len(recipe_ids) > 25 or len(set(recipe_ids)) != len(recipe_ids):
            raise ValueError('Source reader requires 1–25 unique recipes')
        result = {}
        with self.driver.session(default_access_mode=READ_ACCESS, fetch_size=100) as session:
            def read(query, recipe_id, parameter='id'):
                rows = [dict(row) for row in session.run(Query(query, timeout=60), **{parameter: recipe_id})]
                if len(rows) >= 100:
                    raise ValueError('Source result exceeds bounded batch')
                return rows
            for recipe_id in recipe_ids:
                rows = read('''MATCH (r:Recipe) WHERE toString(r.recipe_id)=$id
                    RETURN r.recipe_id AS recipe_id,r.title AS title,r.serves AS serves,r.status AS status,
                    r.duration AS duration,r.instructions AS instructions,r.source AS source LIMIT 2''', recipe_id)
                if len(rows) != 1:
                    raise ValueError('Source recipe not unique')
                snapshot = {'recipe': rows[0],
                    'ingredients': read('''MATCH (r:Recipe)-[e:HAS_INGREDIENT]->(i:Ingredient)
                        WHERE toString(r.recipe_id)=$id RETURN elementId(e) AS edge_id,i.name AS name,
                        e.measurement AS measurement,e.weight_g AS weight_g,e.position AS position
                        ORDER BY edge_id LIMIT 100''', recipe_id),
                    'originals': read('''MATCH (r:Recipe)-[e:HAS_INGREDIENT_ORIGINAL]->(i:Ingredients_original)
                        WHERE toString(r.recipe_id)=$id RETURN e.position AS position,
                        coalesce(i.original_text,i.name) AS text,elementId(e) AS edge_id
                        ORDER BY position,edge_id LIMIT 100''', recipe_id)}
                # Bound the remaining tag fanout before using the application's collect query.
                read('''MATCH (r:Recipe)-[:HAS_TAG]->(t) WHERE toString(r.recipe_id)=$id
                    RETURN elementId(t) AS id LIMIT 100''', recipe_id)
                query = _RECIPE_INFO_QUERY.replace('{match_predicate}', 'toString(r.recipe_id) = $recipe_id').replace('{status_predicate}', 'true')
                semantic = read(query, recipe_id, 'recipe_id')
                if len(semantic) != 1:
                    raise ValueError('Semantic recipe not unique')
                recipe = _record_to_recipe_dict(semantic[0])
                if str(recipe['recipe_id']) != recipe_id:
                    raise ValueError('Semantic source identity differs')
                result[recipe_id] = {'snapshot': snapshot, 'revision': recipe_revision(recipe)}
        return result


def inspect(connection, package: dict, expected_digest: str, read_sources,
            expected_compositions: list[dict]) -> dict:
    """Capture review evidence without locks, queue changes or an apply mode."""
    entries = validate_package(package, expected_digest)
    connection.execute(text('SET TRANSACTION READ ONLY'))
    connection.execute(text("SET LOCAL statement_timeout='60s'"))
    def rows(sql, params=None):
        result = [dict(row) for row in connection.execute(text(sql), params or {}).mappings()]
        if len(result) >= 100:
            raise ValueError('Query result exceeds bounded batch')
        return result
    sources = _sources(entries, read_sources)
    deployment = rows('SELECT release_id,mode FROM matching_deployment WHERE singleton LIMIT 2')
    if deployment != [package['deployment']]:
        raise ValueError('Deployment changed')
    release = entries[0]['release_id']
    if rows('SELECT status FROM matching_releases WHERE release_id=:release LIMIT 2', {'release': release}) != [{'status': 'ready'}]:
        raise ValueError('Release not ready')
    mirrors = []; predecessors = []
    for entry in entries:
        params = {'recipe': entry['recipe_id'], 'source': entry['nutrition_source']}
        active = rows('''SELECT a.run_id,r.profile,r.release_id FROM matching_active_profiles a
            JOIN matching_profile_runs r ON a.run_id=r.run_id AND a.recipe_id=r.recipe_id AND a.nutrition_source=r.nutrition_source
            WHERE a.recipe_id=:recipe AND a.nutrition_source=:source LIMIT 2''', params)
        if len(active) != 1 or active[0]['run_id'] != entry['predecessor_run_id'] or active[0]['release_id'] != entry['release_id'] or digest(active[0]['profile']) != entry['predecessor_profile_digest']:
            raise ValueError('Active predecessor changed')
        mirror = rows('''SELECT row_to_json(p) AS mirror FROM "nutrients-recipe-profiles" p
            WHERE recipe_id=:recipe AND nutrition_source=:source LIMIT 2''', params)
        if len(mirror) != 1:
            raise ValueError('Production mirror missing or duplicated')
        mirrors.append({'recipe_id': entry['recipe_id'], 'nutrition_source': entry['nutrition_source'], 'digest': digest(mirror[0]['mirror'])})
        predecessors.append({'recipe_id': entry['recipe_id'], 'nutrition_source': entry['nutrition_source'], 'run_id': active[0]['run_id']})
    selected = {(COLLECTIONS[d['source_nutrition']], d['selected_candidate_id'])
                for entry in entries for d in entry['profile']['details'] if d.get('selected_candidate_id')}
    expected = {(r['collection'], r['candidate_id']): r['digest'] for r in expected_compositions}
    if len(expected) != len(expected_compositions) or set(expected) != selected:
        raise ValueError('Composition guard coverage differs')
    for (collection, candidate), target in sorted(expected.items()):
        reviewed = reviewed_public_record(candidate)
        if reviewed is not None:
            if digest(reviewed) != target:
                raise ValueError('Selected composition changed')
            continue
        actual = rows('''SELECT * FROM matching_compositions WHERE release_id=:release
            AND collection=:collection AND candidate_id=:candidate LIMIT 2''',
            {'release': release, 'collection': collection, 'candidate': candidate})
        if len(actual) != 1 or digest(actual[0]) != target:
            raise ValueError('Selected composition changed')
    columns = rows('''SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='matching_reprofile_receipts' ORDER BY column_name LIMIT 100''')
    required = {'operation_id','package_digest','status','payload','payload_digest','created_at','updated_at'}
    schema_present = required <= {r['column_name'] for r in columns}
    ids = sorted({entry['recipe_id'] for entry in entries})
    queue = rows('''SELECT recipe_id,generation,completed_generation,last_error FROM matching_projection_jobs
        WHERE recipe_id=ANY(CAST(:ids AS text[])) ORDER BY recipe_id LIMIT 100''', {'ids': ids})
    if _sources(entries, read_sources) != sources:
        raise ValueError('Source changed during preflight')
    return {'version': 'source_reprofile_preflight_v1', 'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'package_digest': expected_digest, 'profiles': len(entries), 'sources': sources,
            'predecessors': predecessors, 'mirror_digests': mirrors, 'composition_digests': expected_compositions,
            'receipt_columns_present': schema_present, 'queue_observation': queue,
            'ready_for_apply': False, 'production_writes': False,
            'remaining_gates': ([] if schema_present else ['Install reviewed receipt migration after approval']) +
                ['Verify pinned deployed runtime artifact', 'Establish actual graph/profile writer and replica/worker control',
                 'Fresh reviewed execution guards and approved one-recipe rehearsal'],
            'global_atomic_snapshot_claimed': False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--expected-package-digest', required=True)
    parser.add_argument('--composition-guards', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Refuse to overwrite preflight evidence')
    package = json.loads(args.package.read_text())
    validate_package(package, args.expected_package_digest)
    guards = json.loads(args.composition_guards.read_text())['checks']
    from scripts.neo4j.reconcile_allergen_journal import _postgres_engine, _graph_driver
    engine = _postgres_engine(); driver = _graph_driver()
    try:
        with engine.connect() as connection, connection.begin():
            report = inspect(connection, package, args.expected_package_digest, SourceReader(driver), guards)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        print(json.dumps({'profiles': report['profiles'], 'ready_for_apply': False, 'production_writes': False}))
    finally:
        driver.close(); engine.dispose()


if __name__ == '__main__':
    main()
