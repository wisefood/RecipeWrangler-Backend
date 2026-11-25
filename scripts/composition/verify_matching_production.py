"""Read-only post-cutover verification of serving data and active graph quantities."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import text

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.utils.nutrition_postgres import get_engine
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.catalog.elastic import get_catalog_client
from scripts.composition.promote_matching_production import reference_fingerprint


def verify() -> dict:
    with get_engine().connect() as c:
        counts=dict(c.execute(text('''SELECT count(*) AS active_profiles,count(DISTINCT recipe_id) AS recipes
            FROM matching_active_profiles''')).mappings().one())
        mismatches=c.execute(text('''SELECT count(*) FROM matching_active_profiles a
            JOIN matching_profile_runs r USING(run_id)
            LEFT JOIN "nutrients-recipe-profiles" p ON p.recipe_id=a.recipe_id AND p.nutrition_source=a.nutrition_source
            WHERE p.recipe_id IS NULL OR p.total_nutrients IS DISTINCT FROM r.profile->'clean_totals'
            OR p.total_nutrients_per_serving IS DISTINCT FROM r.profile->'clean_totals_per_serving'
            OR COALESCE(p.nutri_score,'null'::jsonb) IS DISTINCT FROM r.profile->'nutri_score'
            OR p.nutrition_profiling_details IS DISTINCT FROM r.profile->'details' ''')).scalar()
        regional=c.execute(text('''SELECT count(*) FROM (SELECT a.recipe_id FROM matching_active_profiles a
            JOIN matching_profile_runs r USING(run_id) GROUP BY a.recipe_id
            HAVING count(DISTINCT r.profile->>'quantity_input_hash')>1) s''')).scalar()
        pending=c.execute(text('SELECT count(*) FROM matching_projection_jobs WHERE generation>completed_generation')).scalar()
        runs=[dict(r) for r in c.execute(text('''SELECT r.run_id,r.profile->>'quantity_input_hash' AS hash,
            (SELECT jsonb_agg(jsonb_build_array(d->'position',d->'weight_g') ORDER BY (d->>'position')::int)
             FROM jsonb_array_elements(r.profile->'details') d) AS quantities
            FROM matching_active_profiles a JOIN matching_profile_runs r USING(run_id)''')).mappings()]
        fingerprint=reference_fingerprint(c)
    graph={r['run_id']:dict(r) for r in run_query('''MATCH (o:MatchingIngredientOccurrence) WHERE o.current=true
        WITH o ORDER BY o.position RETURN o.run_id AS run_id,head(collect(o.quantity_input_hash)) AS hash,
        collect([o.position,o.weight_g]) AS quantities''')}
    missing=[];quantity_errors=[]
    for run in runs:
        row=graph.get(run['run_id'])
        if row is None:
            missing.append(run['run_id']);continue
        if row['hash']!=run['hash'] or row['quantities']!=run['quantities']:
            quantity_errors.append(run['run_id'])
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'deployment':store.deployment(),**counts,
        'serving_profile_mismatches':mismatches,'regional_quantity_mismatches':regional,
        'missing_active_graph_runs':len(missing),'graph_quantity_mismatches':len(quantity_errors),
        'graph_active_occurrences':sum(len(r['quantities']) for r in graph.values()),
        'pending_projection_jobs':pending,'reference_fingerprint':fingerprint,
        'catalogue_alias':get_catalog_client()._request('GET','_alias/recipes')}
    report['passed']=not any([mismatches,regional,missing,quantity_errors,pending])
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();report=verify()
    args.report.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    if not report['passed']:raise SystemExit(1)
