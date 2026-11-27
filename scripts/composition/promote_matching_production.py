"""Prepare, atomically promote, or roll back the explicitly authorized matcher."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from sqlalchemy import text

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.matching.quality import finalize_profile, QUALITY_VERSION
from recipe_wrangler.utils.nutrition_postgres import get_engine

REGIONS = ['eu', 'irish', 'hungarian', 'slovenian']
FIELDS = ['total_nutrients','total_nutrients_per_serving','nutri_score','nutri_score_breakdown',
          'nutrition_profiling_details','nutrition_profiling_debug','profiling_quality',
          'trace','pipeline_version','computed_at','updated_at']


def reference_fingerprint(c):
    return c.execute(text('''SELECT md5(string_agg(md5(row_to_json(p)::text),'|' ORDER BY recipe_id,nutrition_source))
        FROM "nutrients-recipe-profiles" p WHERE NOT nutrition_source=ANY(:regions)'''), {'regions': REGIONS}).scalar()


class _Rehearsal(Exception):
    """Raised inside the promotion transaction so a rehearsal rolls everything back."""


def prepare(promotion_id: str, release_id: str, mechanism_version: str | None=None, recipe_ids: list[str] | None=None) -> dict:
    store.release(release_id)
    with get_engine().begin() as c:
        c.execute(text('''INSERT INTO matching_promotions
            (promotion_id,release_id,previous_deployment,previous_active_profiles,status)
            SELECT :id,:release,to_jsonb(d),COALESCE((SELECT jsonb_agg(to_jsonb(a))
            FROM matching_active_profiles a),'[]'::jsonb),'preparing' FROM matching_deployment d
            ON CONFLICT(promotion_id) DO NOTHING'''), {'id': promotion_id, 'release': release_id})
        current=c.execute(text('SELECT status,release_id FROM matching_promotions WHERE promotion_id=:id'),{'id':promotion_id}).mappings().one()
        if current['status'] not in {'preparing','prepared'} or current['release_id']!=release_id:
            raise ValueError('Promotion ID already used or release differs')
    count=0
    with get_engine().connect().execution_options(stream_results=True) as reader:
        runs=reader.execute(text('''SELECT DISTINCT ON(recipe_id,nutrition_source)
            run_id,recipe_id,nutrition_source,profile,mechanism_version FROM matching_profile_runs
            WHERE release_id=:release AND NOT profile ? 'production_prepared_from'
            AND (CAST(:version AS text) IS NULL OR mechanism_version=:version)
            AND (CAST(:ids AS text[]) IS NULL OR recipe_id=ANY(CAST(:ids AS text[])))
            ORDER BY recipe_id,nutrition_source,created_at DESC,run_id DESC'''),
            {'release':release_id,'version':mechanism_version,'ids':recipe_ids}).mappings()
        for batch in runs.partitions(100):
            with get_engine().begin() as c:
                for run in batch:
                    profile=finalize_profile(copy.deepcopy(run['profile']))
                    profile['production_prepared_from']=run['run_id']
                    run_id=store.insert_run(c,run['recipe_id'],run['nutrition_source'],release_id,
                        run['mechanism_version'],{'predecessor':run['run_id'],'quality_version':QUALITY_VERSION},profile)
                    c.execute(text('''INSERT INTO matching_promotion_profiles
                        VALUES(:id,:recipe,:source,:run,:previous)
                        ON CONFLICT(promotion_id,recipe_id,nutrition_source)
                        DO UPDATE SET run_id=EXCLUDED.run_id,source_run_id=EXCLUDED.source_run_id'''),
                        {'id':promotion_id,'recipe':run['recipe_id'],'source':run['nutrition_source'],
                         'run':run_id,'previous':run['run_id']})
                    count+=1
            if count%1000==0:print('Prepared',count,flush=True)
    with get_engine().begin() as c:
        invalid=c.execute(text('''SELECT count(*) FROM (SELECT recipe_id FROM matching_promotion_profiles
            WHERE promotion_id=:id GROUP BY recipe_id HAVING count(*)<>4) s'''),{'id':promotion_id}).scalar()
        if invalid:raise ValueError('Incomplete regional target set')
        c.execute(text("UPDATE matching_promotions SET status='prepared' WHERE promotion_id=:id"),{'id':promotion_id})
    return {'promotion_id':promotion_id,'prepared_profiles':count,'release_id':release_id}


def promote(promotion_id: str, rehearse: bool=False) -> dict:
    try:
        return _promote(promotion_id,rehearse)
    except _Rehearsal as done:
        return {**done.args[0],'rehearsal':True,'committed':False}


def _promote(promotion_id: str, rehearse: bool) -> dict:
    with get_engine().begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('matching_production_promotion',0))"))
        state=c.execute(text('SELECT * FROM matching_promotions WHERE promotion_id=:id FOR UPDATE'),{'id':promotion_id}).mappings().one()
        if state['status']!='prepared':raise ValueError('Promotion must be prepared exactly once')
        deployed=store.deployment()
        # A later promotion replaces the active new-mechanism profiles of the same release.
        if not (deployed['mode']=='legacy' or (deployed['mode']=='new' and deployed['release_id']==state['release_id'])):
            raise ValueError('Expected legacy deployment, or new mode on the same release')
        # Freeze the read surface while its rollback snapshot and replacement are written.
        c.execute(text('LOCK TABLE "nutrients-recipe-profiles" IN SHARE ROW EXCLUSIVE MODE'))
        before=reference_fingerprint(c)
        c.execute(text('''INSERT INTO matching_production_backups
            SELECT :id,p.recipe_id,p.nutrition_source,to_jsonb(p)
            FROM "nutrients-recipe-profiles" p JOIN matching_promotion_profiles t
            ON t.recipe_id=p.recipe_id AND t.nutrition_source=p.nutrition_source AND t.promotion_id=:id'''), {'id':promotion_id})
        expected=c.execute(text('SELECT count(*) FROM matching_promotion_profiles WHERE promotion_id=:id'),{'id':promotion_id}).scalar()
        backups=c.execute(text('SELECT count(*) FROM matching_production_backups WHERE promotion_id=:id'),{'id':promotion_id}).scalar()
        if not expected or expected!=backups:raise ValueError('Every target must have a full predecessor backup')
        changed=c.execute(text('''UPDATE "nutrients-recipe-profiles" p SET
            total_nutrients=r.profile->'clean_totals',
            total_nutrients_per_serving=r.profile->'clean_totals_per_serving',
            nutri_score=NULLIF(r.profile->'nutri_score','null'::jsonb),
            nutri_score_breakdown=NULLIF(r.profile->'nutri_score_breakdown','null'::jsonb),
            nutrition_profiling_details=r.profile->'details',
            profiling_quality=COALESCE(p.profiling_quality,'{}'::jsonb)||(r.profile->'profiling_quality'),
            nutrition_profiling_debug=COALESCE(p.nutrition_profiling_debug,'{}'::jsonb)||
                jsonb_build_object('matching_run_id',r.run_id,'nutrition_quality',r.profile->'nutrition_quality',
                    'profiling_quality',r.profile->'profiling_quality'),
            trace=COALESCE(p.trace,'{}'::jsonb)||jsonb_build_object('matching_run_id',r.run_id,
                'profiling_quality',COALESCE(p.profiling_quality,'{}'::jsonb)||(r.profile->'profiling_quality')),
            pipeline_version=r.mechanism_version,computed_at=now(),updated_at=now()
            FROM matching_promotion_profiles t JOIN matching_profile_runs r USING(run_id)
            WHERE t.promotion_id=:id AND p.recipe_id=t.recipe_id AND p.nutrition_source=t.nutrition_source'''), {'id':promotion_id}).rowcount
        if changed!=expected:raise ValueError('Promotion count mismatch')
        c.execute(text('''INSERT INTO matching_active_profiles(recipe_id,nutrition_source,run_id)
            SELECT recipe_id,nutrition_source,run_id FROM matching_promotion_profiles WHERE promotion_id=:id
            ON CONFLICT(recipe_id,nutrition_source) DO UPDATE SET run_id=EXCLUDED.run_id'''), {'id':promotion_id})
        c.execute(text("UPDATE matching_deployment SET mode='new',release_id=:release,updated_at=now() WHERE singleton"),{'release':state['release_id']})
        c.execute(text("UPDATE matching_promotions SET status='active' WHERE promotion_id=:id"),{'id':promotion_id})
        after=reference_fingerprint(c)
        if after!=before:raise ValueError('Ground reference rows changed')
        # Graph, claims, catalogue and caches follow through the existing projection worker.
        queued=_enqueue(c,promotion_id)
        result={'promotion_id':promotion_id,'activated_profiles':changed,'reference_fingerprint_before':before,
                'reference_fingerprint_after':after,'release_id':state['release_id'],'queued_recipes':queued}
        if rehearse:raise _Rehearsal(result)
    return result


def _enqueue(c,promotion_id):
    recipes=[r[0] for r in c.execute(text('''SELECT DISTINCT recipe_id FROM matching_promotion_profiles
        WHERE promotion_id=:id'''),{'id':promotion_id})]
    for recipe_id in recipes:store.enqueue_projection(c,recipe_id)
    return len(recipes)


def rollback(promotion_id: str) -> dict:
    with get_engine().begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('matching_production_promotion',0))"))
        state=c.execute(text('SELECT * FROM matching_promotions WHERE promotion_id=:id FOR UPDATE'),{'id':promotion_id}).mappings().one()
        if state['status']!='active':raise ValueError('Only an active promotion can be rolled back')
        advanced=c.execute(text('''SELECT count(*) FROM matching_active_profiles a FULL JOIN
            (SELECT recipe_id,nutrition_source,run_id FROM matching_promotion_profiles
             WHERE promotion_id=:id) t USING(recipe_id,nutrition_source)
            WHERE a.run_id IS DISTINCT FROM t.run_id'''), {'id':promotion_id}).scalar()
        if advanced:
            raise ValueError('Profiles changed after activation; reconcile those writes before rollback')
        c.execute(text('LOCK TABLE "nutrients-recipe-profiles" IN SHARE ROW EXCLUSIVE MODE'))
        assignments=','.join(f'"{field}"=old."{field}"' for field in FIELDS)
        count=c.execute(text('''UPDATE "nutrients-recipe-profiles" p SET '''+assignments+'''
            FROM matching_production_backups b CROSS JOIN LATERAL
            jsonb_populate_record(NULL::"nutrients-recipe-profiles",b.previous_row) old
            WHERE b.promotion_id=:id AND p.recipe_id=b.recipe_id AND p.nutrition_source=b.nutrition_source'''),{'id':promotion_id}).rowcount
        c.execute(text('DELETE FROM matching_active_profiles'))
        if state['previous_active_profiles']:
            c.execute(text('''INSERT INTO matching_active_profiles
                SELECT * FROM jsonb_populate_recordset(NULL::matching_active_profiles,CAST(:rows AS jsonb))'''),
                {'rows':json.dumps(state['previous_active_profiles'])})
        c.execute(text('UPDATE matching_deployment SET mode=:mode,release_id=:release,updated_at=now() WHERE singleton'),
                  {'mode':state['previous_deployment']['mode'],'release':state['previous_deployment']['release_id']})
        c.execute(text("UPDATE matching_promotions SET status='rolled_back' WHERE promotion_id=:id"),{'id':promotion_id})
        queued=_enqueue(c,promotion_id)
    return {'promotion_id':promotion_id,'restored_profiles':count,'queued_recipes':queued}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','promote','rollback'])
    parser.add_argument('--promotion-id',required=True)
    parser.add_argument('--release-id',default='2026-10-27-v11')
    parser.add_argument('--mechanism-version')
    parser.add_argument('--recipe-ids',type=Path,help='file of recipe ids; prepare only these')
    parser.add_argument('--rehearse',action='store_true',help='promote inside a transaction that is rolled back')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    report=(prepare(args.promotion_id,args.release_id,args.mechanism_version,args.recipe_ids.read_text().split() if args.recipe_ids else None) if args.action=='prepare'
            else promote(args.promotion_id,args.rehearse) if args.action=='promote' else rollback(args.promotion_id))
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
