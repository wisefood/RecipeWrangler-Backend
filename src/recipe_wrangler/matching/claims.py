"""Refresh deterministic recipe claims from the active global calculation."""
from __future__ import annotations

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_claims import compute_nutrition_claim_tags, infer_physical_form, NUTRITION_CLAIM_TAG_NAMES
from recipe_wrangler.utils.nutrition_postgres import get_engine
from recipe_wrangler.utils.neo4j_utils import run_query


def refresh_claims(recipe_ids: list[str] | None = None) -> int:
    query='''SELECT r.recipe_id,r.profile FROM matching_active_profiles a
        JOIN matching_profile_runs r USING(run_id) WHERE a.nutrition_source='eu' '''
    if recipe_ids is not None:
        query+=' AND a.recipe_id=ANY(:ids)'
    count=0
    with get_engine().connect().execution_options(stream_results=True) as c:
        for batch in c.execute(text(query),{'ids':recipe_ids}).mappings().partitions(100):
            updates=[]
            for row in batch:
                p=row['profile']
                flags=(p.get('nutrition_quality') or {}).get('diagnostic_flags')
                claims=[] if flags else compute_nutrition_claim_tags(p.get('clean_totals'),
                    p.get('details'),p.get('nutri_score'),infer_physical_form(p.get('title')))
                updates.append({'id':row['recipe_id'],'claims':claims})
            run_query('''UNWIND $rows AS row
                MATCH (r:Recipe {recipe_id:row.id})
                OPTIONAL MATCH (r)-[edge:HAS_TAG]->(t:Tag) WHERE t.name IN $names
                WITH r,row,collect(edge) AS obsolete
                FOREACH (edge IN obsolete | DELETE edge)
                FOREACH (name IN row.claims |
                    MERGE (t:Tag {name:name}) SET t.category='nutrition_claim'
                    MERGE (r)-[:HAS_TAG]->(t))
                SET r.matching_claims_source='eu',r.matching_claims_version='active_matching_profile_v1' ''',
                {'rows':updates,'names':list(NUTRITION_CLAIM_TAG_NAMES)})
            count+=len(updates)
    return count


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',required=True)
    parser.parse_args()
    print('Refreshed nutrition claims for',refresh_claims(),'recipes')
