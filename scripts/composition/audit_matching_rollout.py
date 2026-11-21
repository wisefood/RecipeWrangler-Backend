"""Verify rollout links and report incomplete work from database evidence."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine
from recipe_wrangler.utils.neo4j_utils import run_query


def audit(release_id):
    with get_engine().connect() as c:
        result={'release_id':release_id}
        result['compositions']=c.execute(text('SELECT count(*) FROM matching_compositions WHERE release_id=:r'),{'r':release_id}).scalar()
        result['grounded_frames']=c.execute(text('SELECT count(*) FROM matching_ingredient_frames WHERE release_id=:r'),{'r':release_id}).scalar()
        result['eligible_profiles']=c.execute(text('''SELECT count(*) FROM "nutrients-recipe-profiles"
            WHERE nutrition_source IN ('eu','irish','hungarian','slovenian')''')).scalar()
        result['completed_profiles']=c.execute(text('''SELECT count(DISTINCT (recipe_id,nutrition_source))
            FROM matching_profile_runs WHERE release_id=:r'''),{'r':release_id}).scalar()
        current=[dict(r) for r in c.execute(text('''WITH latest AS (
            SELECT DISTINCT ON(recipe_id,nutrition_source) recipe_id,nutrition_source,profile
                ,run_id FROM matching_profile_runs WHERE release_id=:r
            ORDER BY recipe_id,nutrition_source,created_at DESC,run_id DESC)
            SELECT recipe_id,run_id,profile->>'quantity_input_hash' AS hash,
                (SELECT jsonb_agg(d->'weight_g' ORDER BY (d->>'position')::int)
                    FROM jsonb_array_elements(profile->'details') d) AS weights FROM latest'''),{'r':release_id}).mappings()]
        current_runs={r['run_id'] for r in current}
        recipe_quantities=defaultdict(set)
        for r in current:recipe_quantities[r['recipe_id']].add((r['hash'],tuple(r['weights'] or [])))
        result['quantity_invariant_failures']=sum(len(v)!=1 or any(h is None for h,w in v)
            for v in recipe_quantities.values())
        result['active_profiles']=c.execute(text('SELECT count(*) FROM matching_active_profiles')).scalar()
        result['legacy_total_mismatches']=c.execute(text('''SELECT count(*) FROM matching_profile_runs r
            JOIN "nutrients-recipe-profiles" p USING(recipe_id,nutrition_source)
            WHERE r.mechanism_version='legacy_snapshot_complete_v2'
            AND (r.profile->'total_nutrients' IS DISTINCT FROM p.total_nutrients
            OR r.profile->'total_nutrients_per_serving' IS DISTINCT FROM p.total_nutrients_per_serving)''')).scalar()
        concepts=set(c.execute(text('SELECT concept_id FROM matching_concepts')).scalars())
    graph=lambda query:[dict(r) for r in run_query(query,{'release':release_id})]
    result['variants']=graph('MATCH (v:MatchingFoodVariant {release_id:$release}) RETURN count(v) AS n')[0]['n']
    graph_runs={r['run_id'] for r in graph('MATCH (o:MatchingIngredientOccurrence {release_id:$release}) RETURN DISTINCT o.run_id AS run_id')}
    result['historical_occurrence_runs']=len(graph_runs-current_runs)
    result['occurrence_runs']=len(graph_runs&current_runs)
    projected={r['run_id']:r for r in graph('''MATCH (o:MatchingIngredientOccurrence {release_id:$release})
        WHERE o.quantity_input_hash IS NOT NULL
        WITH o ORDER BY o.run_id,o.position
        RETURN o.run_id AS run_id,collect(o.weight_g) AS weights,
            collect(DISTINCT o.quantity_input_hash) AS hashes''')}
    result['graph_quantity_mismatches']=sum(
        r['run_id'] not in projected or projected[r['run_id']]['weights']!=r['weights']
        or projected[r['run_id']]['hashes']!=[r['hash']] for r in current)
    result['selected_occurrences_without_variant']=graph('''MATCH (o:MatchingIngredientOccurrence {release_id:$release})
        WHERE o.outcome<>'unresolved' AND NOT EXISTS {(o)-[:MATCHED_TO_VARIANT]->()}
        RETURN count(o) AS n''')[0]['n']
    result['chicken_breast']=graph('''MATCH (i:Ingredient {name:'chicken breast'})-[:HAS_MATCHING_FRAME]->(f:MatchingIngredientFrame {release_id:$release})
        MATCH (f)-[:HAS_IDENTITY]->(identity:MatchingFoodIdentity)
        OPTIONAL MATCH (f)-[:HAS_OBSERVED_FACET]->(p:MatchingFacet)
        RETURN identity.name AS identity,collect(DISTINCT {kind:p.kind,value:p.value}) AS facets''')
    graph_concepts=graph('MATCH (f:MatchingFacet) RETURN DISTINCT f.postgres_concept_id AS id')
    result['graph_concepts_missing_registry']=[r['id'] for r in graph_concepts if r['id'] and r['id'] not in concepts]
    result['backfill_pending']=result['eligible_profiles']-result['completed_profiles']
    result['graph_runs_pending']=result['completed_profiles']-result['occurrence_runs']
    result['activation_status']='not_activated' if not result['active_profiles'] else 'has_active_profiles'
    folder=Path('analysis')/('matching_graph_audit_'+release_id);folder.mkdir(exist_ok=True)
    (folder/'rollout_verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='graph_concepts_missing_registry'},indent=2))
    print('Graph concepts missing registry',len(result['graph_concepts_missing_registry']))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    audit(p.parse_args().release_id)
