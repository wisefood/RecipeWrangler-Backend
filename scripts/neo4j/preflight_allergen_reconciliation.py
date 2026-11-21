"""Fresh read-only journal preflight. No mutation or apply mode exists."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from scripts.neo4j.preview_allergen_reconciliation import (
    QUERIES, canonical, digest, encoded, read_rows,
)


def profile_state(recipe_ids: list[str], deadline: float) -> dict:
    """Serial small result pages in a read-only PostgreSQL transaction."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from recipe_wrangler.utils.nutrition_postgres import _get_config

    cfg = _get_config()
    connection = psycopg2.connect(dbname=cfg['db_name'], user=cfg['db_user'], password=cfg['db_password'],
                                  host=cfg['db_host'], port=cfg['db_port'], connect_timeout=5)
    result = {'profiles':[], 'queue':[], 'deployment':[]}
    try:
        connection.set_session(isolation_level='REPEATABLE READ', readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout='60s'")
            cursor.execute('SELECT release_id,mode FROM matching_deployment WHERE singleton')
            result['deployment'] = [dict(r) for r in cursor.fetchall()]
            after = ('', '')
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError('Preflight total budget exceeded')
                cursor.execute('''SELECT a.recipe_id,a.nutrition_source,a.run_id,r.release_id,r.input_hash,
                    md5(r.profile::text) AS profile_fingerprint
                    FROM matching_active_profiles a JOIN matching_profile_runs r USING(run_id)
                    WHERE a.recipe_id=ANY(%s) AND (a.recipe_id,a.nutrition_source)>(%s,%s)
                    ORDER BY a.recipe_id,a.nutrition_source LIMIT 100''', (recipe_ids, *after))
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    break
                result['profiles'].extend(rows)
                after = (rows[-1]['recipe_id'], rows[-1]['nutrition_source'])
            after_id = ''
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError('Preflight total budget exceeded')
                cursor.execute('''SELECT recipe_id,generation,completed_generation,attempts,
                    (last_error IS NOT NULL) AS has_error
                    FROM matching_projection_jobs WHERE recipe_id=ANY(%s) AND recipe_id>%s
                    ORDER BY recipe_id LIMIT 100''', (recipe_ids, after_id))
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    break
                result['queue'].extend(rows)
                after_id = rows[-1]['recipe_id']
    finally:
        connection.rollback()
        connection.close()
    return result


def compare_journal(journal: dict, ingredients: list[dict], recipes: list[dict], classes: list[dict]) -> dict:
    """Reject identity, evidence, membership, recipe or ontology-target drift."""
    issues = []
    by_node = {r['element_id']:r for r in ingredients}
    by_recipe = {r['element_id']:r for r in recipes}
    by_class = {r['element_id']:r for r in classes}
    expected_ids = {r['ingredient_id'] for r in journal['cases']}
    observed_ids = [r['properties'].get('canonical_id') for r in ingredients]
    if len(observed_ids) != len(expected_ids) or set(observed_ids) != expected_ids:
        issues.append({'kind':'ingredient_identity_missing_or_duplicated'})
    for case in journal['cases']:
        row = by_node.get(case['element_id'])
        if row is None or digest(row) != case['before_sha256']:
            issues.append({'kind':'ingredient_before_drift', 'name':case['name']})
        target = case['link_operation']['target_element_id']
        if target not in by_class or digest(by_class[target]) != case['link_operation']['target_class_before_sha256']:
            issues.append({'kind':'target_class_drift', 'name':case['name']})
        target_id = case['link_operation']['target_foodon_id']
        if sum(r['properties'].get('foodon_id') == target_id for r in classes) != 1:
            issues.append({'kind':'target_class_not_unique', 'name':case['name']})
        memberships = sorted(r['element_id'] for r in recipes
                             if case['element_id'] in {i['target'] for i in r['ingredients']})
        if memberships != case['guarded_recipe_membership']:
            issues.append({'kind':'inverse_membership_drift', 'name':case['name'],
                           'added':sorted(set(memberships)-set(case['guarded_recipe_membership'])),
                           'removed':sorted(set(case['guarded_recipe_membership'])-set(memberships))})
    expected_recipe_ids = {r['element_id'] for r in journal['affected_recipes']}
    if set(by_recipe) != expected_recipe_ids:
        issues.append({'kind':'affected_recipe_set_drift', 'added':sorted(set(by_recipe)-expected_recipe_ids),
                       'removed':sorted(expected_recipe_ids-set(by_recipe))})
    for guard in journal['affected_recipes']:
        row = by_recipe.get(guard['element_id'])
        if row is None or digest(row) != guard['before_sha256']:
            issues.append({'kind':'recipe_before_drift', 'recipe_id':guard['recipe_id']})
    return {'passed':not issues, 'issues':issues, 'ingredients':len(ingredients),
            'recipes':len(recipes), 'classes':len(classes)}


def preflight(journal_path: Path, out: Path, *, smoke: bool = False) -> dict:
    from neo4j import GraphDatabase, READ_ACCESS, unit_of_work
    from recipe_wrangler.utils.env_loader import load_runtime_env

    journal = json.loads(journal_path.read_text())
    out.mkdir(parents=True, exist_ok=False)
    load_runtime_env()
    logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
    auth = os.getenv('NEO4J_AUTH')
    pair = tuple(auth.split('/', 1)) if auth else (
        os.getenv('NEO4J_USERNAME') or os.getenv('NEO4J_USER'), os.getenv('NEO4J_PASSWORD'))
    ingredient_ids = [r['ingredient_id'] for r in journal['cases']]
    queries = dict(QUERIES)
    queries['ingredients'] = queries['ingredients'].replace(
        'WHERE elementId(i) > $after', 'WHERE elementId(i) > $after AND i.canonical_id IN $ingredient_ids')
    queries['recipes'] = queries['recipes'].replace(
        'WHERE elementId(r) > $after', '''WHERE elementId(r) > $after AND EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(selected:Ingredient)
        WHERE selected.canonical_id IN $ingredient_ids }''')
    queries['context_ingredients'] = QUERIES['ingredients'].replace(
        'WHERE elementId(i) > $after', '''WHERE elementId(i) > $after AND EXISTS {
        MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i)
        WHERE EXISTS { MATCH (r)-[:HAS_INGREDIENT]->(selected:Ingredient)
                       WHERE selected.canonical_id IN $ingredient_ids } }''')
    previous_classes = read_rows(Path(journal['rollback_inputs']['classes']))
    previous_class_hash = digest(previous_classes)
    rows_by_pass = []
    metrics = []
    start = time.monotonic()
    postgres_before = profile_state([r['recipe_id'] for r in journal['affected_recipes']], start+600) if not smoke else None
    with GraphDatabase.driver(os.getenv('NEO4J_URI', 'bolt://localhost:7687'), auth=pair,
                              max_connection_pool_size=1, max_transaction_retry_time=0,
                              connection_timeout=5, connection_acquisition_timeout=5) as driver:
        with driver.session(default_access_mode=READ_ACCESS, fetch_size=100) as session:
            for iteration in range(1 if smoke else 2):
                captured = {}
                for kind, query in queries.items():
                    after = ''
                    rows = []
                    while True:
                        if time.monotonic()-start > 600:
                            raise TimeoutError('Ten-minute preflight budget exceeded')
                        @unit_of_work(timeout=60)
                        def read_page(tx):
                            return [r.data() for r in tx.run(query, after=after, limit=3 if smoke else 100,
                                                            ingredient_ids=ingredient_ids)]
                        tick = time.monotonic()
                        page = session.execute_read(read_page)
                        metrics.append({'kind':kind, 'pass':iteration, 'rows':len(page),
                                        'seconds':time.monotonic()-tick})
                        if not page:
                            break
                        rows.extend(canonical(r) for r in page)
                        next_after = page[-1]['element_id']
                        if next_after <= after:
                            raise ValueError('Pagination did not advance')
                        after = next_after
                        if smoke:
                            break
                    captured[kind] = rows
                    with gzip.open(out / f'{kind}.{iteration}.jsonl.gz', 'wt') as stream:
                        for row in rows:
                            stream.write(encoded(row)+'\n')
                rows_by_pass.append(captured)
    if not smoke:
        # Close the graph driver before opening the next database connection.
        recipe_ids = [str(r['properties'].get('recipe_id') or r['properties'].get('id'))
                      for r in rows_by_pass[-1]['recipes']]
        rows_by_pass[0]['postgres_state'] = postgres_before
        rows_by_pass[-1]['postgres_state'] = profile_state(recipe_ids, start+600)
        for iteration, captured in enumerate(rows_by_pass):
            (out / f'postgres_state.{iteration}.json').write_text(
                json.dumps(captured['postgres_state'], indent=2, default=str)+'\n')
    result = {'writes_executed':False, 'smoke_only':smoke, 'journal_sha256':hashlib.sha256(journal_path.read_bytes()).hexdigest(),
              'query_timeout_seconds':60, 'max_page_rows':100, 'metrics':metrics,
              'elapsed_seconds':time.monotonic()-start}
    if not smoke:
        result.update(compare_journal(journal, **{k:rows_by_pass[0][k] for k in ['ingredients', 'recipes', 'classes']}))
        result['stable_repeated_capture'] = digest(rows_by_pass[0]) == digest(rows_by_pass[1])
        result['complete_ontology_unchanged'] = digest(rows_by_pass[0]['classes']) == previous_class_hash
        result['passed'] &= result['stable_repeated_capture'] and result['complete_ontology_unchanged']
        result['context_ingredients'] = len(rows_by_pass[0]['context_ingredients'])
        result['active_profiles'] = len(rows_by_pass[0]['postgres_state']['profiles'])
        result['pending_jobs'] = sum(r['generation'] > r['completed_generation']
                                     for r in rows_by_pass[0]['postgres_state']['queue'])
    (out / 'preflight.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'metrics'}, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    result = preflight(args.journal, args.out, smoke=args.smoke)
    if not args.smoke and not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
