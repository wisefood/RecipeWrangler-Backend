"""Apply an explicit positional source review, archiving surplus edges for rollback.

Default is a transactional rehearsal. Never selects duplicates by text alone.
"""
import argparse
import json
from pathlib import Path
from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()
from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id


def review_state(recipe: dict) -> dict:
    # Public nutrition tags are derived from the active profile; this writer
    # owns source occurrences/instructions and never writes those tags.
    return {key: value for key, value in recipe.items() if key != 'tags'}


def apply_review(review: dict, backup: Path, *, commit: bool = False) -> dict:
    from recipe_wrangler.utils.neo4j_utils import driver
    rid = review['recipe_id']
    assert not review['applied'] and not backup.exists()
    assert review_state(fetch_recipe_info_by_id(rid)) == review_state(review['before']), 'Source changed since review'
    bindings = review['occurrence_bindings']
    kept = {b['original_position']: b for b in bindings}
    surplus = set(review['surplus_positions'])
    assert len(kept) == len(bindings)
    assert not surplus.intersection(kept)
    assert sorted([*kept, *surplus]) == list(range(len(review['before']['original_ingredients'])))
    assert [b['position'] for b in bindings] == list(range(len(bindings)))
    assert [b['text'] for b in bindings] == review['after_original_ingredients']
    operation = backup.stem
    with driver.session() as session:
        tx = session.begin_transaction(timeout=60)
        try:
            tx.run('MATCH (r:Recipe {recipe_id:$rid}) SET r.instructions=r.instructions', rid=rid).consume()
            rows = [dict(row) for row in tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->(o)
                RETURN elementId(rel) AS edge, elementId(o) AS node, properties(rel) AS properties,
                    rel.position AS position, coalesce(o.original_text,o.name) AS text
                ORDER BY position LIMIT 100''', rid=rid)]
            assert [r['position'] for r in rows] == list(range(len(review['before']['original_ingredients'])))
            assert [r['text'] for r in rows] == review['before']['original_ingredients']
            instructions = tx.run('MATCH (r:Recipe {recipe_id:$rid}) RETURN r.instructions AS instructions',rid=rid).single()['instructions']
            assert instructions == review['before']['instructions']
            payload = {'operation':operation,'review':review,'original_edges':rows,'committed':False}
            with backup.open('x') as f:json.dump(payload,f,indent=2,ensure_ascii=False)
            for row in rows:
                if row['position'] in surplus:
                    tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->(o)
                        WHERE elementId(rel)=$edge
                        CREATE (r)-[archived:HAS_INGREDIENT_ORIGINAL_ARCHIVE]->(o)
                        SET archived=$properties, archived.source_review_id=$operation
                        DELETE rel''',rid=rid,edge=row['edge'],properties=row['properties'],operation=operation).consume()
                else:
                    tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->()
                        WHERE elementId(rel)=$edge SET rel.position=$position''',rid=rid,edge=row['edge'],
                        position=kept[row['position']]['position']).consume()
            tx.run('MATCH (r:Recipe {recipe_id:$rid}) SET r.instructions=$instructions',rid=rid,
                   instructions=review['after_instructions']).consume()
            after = [row['text'] for row in tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->(o)
                RETURN coalesce(o.original_text,o.name) AS text ORDER BY rel.position LIMIT 100''',rid=rid)]
            assert after == review['after_original_ingredients']
            if commit:tx.commit()
            else:tx.rollback()
        finally:
            tx.close()
    current = fetch_recipe_info_by_id(rid)
    expected = dict(review['before'], original_ingredients=review['after_original_ingredients'],
                    instructions=review['after_instructions']) if commit else review['before']
    assert review_state(current) == review_state(expected), 'Source verification failed'
    payload['committed']=commit
    backup.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n')
    return {'recipe_id':rid,'status':'applied' if commit else 'rehearsed_rolled_back','archived_edges':len(surplus)}


def rollback(backup: Path) -> dict:
    from recipe_wrangler.utils.neo4j_utils import driver
    payload=json.loads(backup.read_text());review=payload['review'];rid=review['recipe_id']
    assert payload['committed']
    assert review_state(fetch_recipe_info_by_id(rid))==review_state(dict(review['before'],original_ingredients=review['after_original_ingredients'],instructions=review['after_instructions']))
    surplus=set(review['surplus_positions'])
    with driver.session() as session, session.begin_transaction(timeout=60) as tx:
        tx.run('MATCH (r:Recipe {recipe_id:$rid}) SET r.instructions=r.instructions',rid=rid).consume()
        current = [row['text'] for row in tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->(o)
            RETURN coalesce(o.original_text,o.name) AS text ORDER BY rel.position LIMIT 100''',rid=rid)]
        assert current == review['after_original_ingredients'], 'Source changed before rollback lock'
        instructions = tx.run('MATCH (r:Recipe {recipe_id:$rid}) RETURN r.instructions AS instructions',rid=rid).single()['instructions']
        assert instructions == review['after_instructions']
        for row in payload['original_edges']:
            if row['position'] in surplus:
                record=tx.run('''MATCH (r:Recipe {recipe_id:$rid})-[archived:HAS_INGREDIENT_ORIGINAL_ARCHIVE]->(o)
                    WHERE elementId(o)=$node AND archived.source_review_id=$operation
                    CREATE (r)-[rel:HAS_INGREDIENT_ORIGINAL]->(o) SET rel=$properties DELETE archived
                    RETURN count(rel) AS count''',rid=rid,node=row['node'],operation=payload['operation'],properties=row['properties']).single()
            else:
                record=tx.run('''MATCH (:Recipe {recipe_id:$rid})-[rel:HAS_INGREDIENT_ORIGINAL]->()
                    WHERE elementId(rel)=$edge SET rel=$properties RETURN count(rel) AS count''',
                    rid=rid,edge=row['edge'],properties=row['properties']).single()
            assert record['count']==1
        tx.run('MATCH (r:Recipe {recipe_id:$rid}) SET r.instructions=$instructions',rid=rid,instructions=review['before']['instructions']).consume()
        tx.commit()
    assert review_state(fetch_recipe_info_by_id(rid))==review_state(review['before'])
    return {'recipe_id':rid,'status':'rolled_back'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path)
    parser.add_argument('--recipe-id')
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args();load_runtime_env()
    if args.rollback:
        assert args.apply
        result=rollback(args.backup)
    else:
        review=next(r for r in json.loads(args.review.read_text()) if r['recipe_id']==args.recipe_id)
        result=apply_review(review,args.backup,commit=args.apply)
    print(json.dumps(result),flush=True)
