"""Prepare, activate or roll back scoped, four-region source-context repairs."""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from sqlalchemy import text

from recipe_wrangler.matching.quality import finalize_profile
from recipe_wrangler.matching.source_context import recipe_revision
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
from recipe_wrangler.utils.nutrition_postgres import get_engine

REGIONS={'eu','irish','hungarian','slovenian'}
FIELDS=('total_nutrients','total_nutrients_per_serving','nutri_score','nutri_score_breakdown',
    'nutrition_profiling_details','profiling_quality','nutrition_profiling_debug','trace',
    'pipeline_version','computed_at','updated_at')


def _reference_fingerprint(connection):
    return connection.execute(text('''SELECT md5(string_agg(md5(row_to_json(p)::text),'|' ORDER BY recipe_id,nutrition_source))
        FROM "nutrients-recipe-profiles" p WHERE NOT nutrition_source=ANY(:sources)'''),{'sources':sorted(REGIONS)}).scalar()


def prepare(reports: list[dict]) -> list[dict]:
    """Accept only complete four-region review sets pinned to current predecessors."""
    entries=[];groups={};recipes={};seen=set()
    for report in reports:
        for item in report['profiles']:
            recipe_id,source=item['recipe_id'],item['nutrition_source']
            if source not in REGIONS or (recipe_id,source) in seen:
                raise ValueError('Duplicate or unsupported repair profile')
            seen.add((recipe_id,source))
            active=store.active_profile(recipe_id,source,include_provenance=True)
            if active is None or active['run_id']!=item['baseline_run_id']:
                raise ValueError('Active predecessor changed')
            if item['missing_positions']:
                raise ValueError('Cannot activate incomplete original source context')
            candidate=item.get('recalculated_profile')
            if candidate is None:
                if item['status']!='already_complete':
                    raise ValueError('Changed source context must have a recalculated review profile')
                candidate=active['profile']
            profile=deepcopy(candidate)
            if profile.get('catalogue_release')!=active['release_id']:
                raise ValueError('Repair catalogue release differs')
            if len(profile['details'])!=len(active['profile']['details']):
                raise ValueError('Repair occurrence count differs')
            if profile.get('serves')!=active['profile'].get('serves'):
                raise ValueError('Repair changes servings')
            vector=[]
            for position,(detail,old) in enumerate(zip(profile['details'],active['profile']['details'],strict=True)):
                if (detail['position']!=position or detail['ingredient']!=old['ingredient'] or
                    detail['weight_g']!=old['weight_g'] or not detail.get('original_ingredient_text')):
                    raise ValueError('Repair changes identity, order, weight or drops source text')
                if detail.get('quantity_input_hash')!=profile['quantity_input_hash']:
                    raise ValueError('Repair occurrence hash differs')
                vector.append([detail['ingredient'],detail['weight_g'],detail['original_ingredient_text']])
            if recipe_id not in recipes:
                recipes[recipe_id]=fetch_recipe_info_by_id(recipe_id)
                if str(recipes[recipe_id].get('recipe_id'))!=recipe_id:
                    raise ValueError('Authoritative recipe unavailable')
            profile['source_recipe_revision']=recipe_revision(recipes[recipe_id])
            profile['source_context_repair_of_run_id']=active['run_id']
            profile['source_context_repair_version']='original_context_repair_v1'
            profile['source_context_evidence']={'canonical_file':report.get('canonical_file'),
                'canonical_digest':report.get('canonical_digest'),'raw_source_recovery':item.get('raw_source_recovery'),
                'revised_canonical_inputs':item.get('revised_canonical_inputs')}
            profile=finalize_profile(profile)
            groups.setdefault(recipe_id,[]).append((source,store.digest(vector),profile['quantity_input_hash'],active['release_id']))
            entries.append({'recipe_id':recipe_id,'nutrition_source':source,'old_run_id':active['run_id'],
                'release_id':active['release_id'],'profile':profile})
    if not entries:raise ValueError('No repair profiles')
    for group in groups.values():
        if {item[0] for item in group}!=REGIONS or len({item[1:] for item in group})!=1:
            raise ValueError('Repair must have identical four-region quantity/source vectors')
    return entries


def _mirror(connection,recipe_id,source,run_id):
    changed=connection.execute(text('''UPDATE "nutrients-recipe-profiles" p SET
        total_nutrients=r.profile->'clean_totals',total_nutrients_per_serving=r.profile->'clean_totals_per_serving',
        nutri_score=NULLIF(r.profile->'nutri_score','null'::jsonb),
        nutri_score_breakdown=NULLIF(r.profile->'nutri_score_breakdown','null'::jsonb),
        nutrition_profiling_details=r.profile->'details',
        profiling_quality=COALESCE(p.profiling_quality,'{}'::jsonb)||(r.profile->'profiling_quality'),
        nutrition_profiling_debug=COALESCE(p.nutrition_profiling_debug,'{}'::jsonb)||jsonb_build_object('matching_run_id',r.run_id),
        trace=COALESCE(p.trace,'{}'::jsonb)||jsonb_build_object('matching_run_id',r.run_id,
            'profiling_quality',COALESCE(p.profiling_quality,'{}'::jsonb)||(r.profile->'profiling_quality')),
        pipeline_version=r.mechanism_version,computed_at=now(),updated_at=now()
        FROM matching_profile_runs r WHERE r.run_id=:run AND p.recipe_id=:recipe AND p.nutrition_source=:source'''),
        {'run':run_id,'recipe':recipe_id,'source':source}).rowcount
    if changed!=1:raise ValueError('Production mirror missing')


def activate(entries: list[dict], backup: Path) -> dict:
    if backup.exists():raise ValueError('Backup already exists; use a fresh operation path')
    report={'version':'original_context_activation_v1','status':'prepared','entries':[]}
    with get_engine().begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('matching_production_promotion',0))"))
        deployment=connection.execute(text('SELECT release_id,mode FROM matching_deployment WHERE singleton FOR UPDATE')).mappings().one()
        if deployment['mode']!='new' or any(item['release_id']!=deployment['release_id'] for item in entries):
            raise ValueError('Deployment does not match scoped repair')
        before=_reference_fingerprint(connection)
        for item in entries:
            params={'recipe':item['recipe_id'],'source':item['nutrition_source']}
            current=connection.execute(text('SELECT run_id FROM matching_active_profiles WHERE recipe_id=:recipe AND nutrition_source=:source FOR UPDATE'),params).scalar()
            if current!=item['old_run_id']:raise ValueError('Active predecessor changed before activation')
            if recipe_revision(fetch_recipe_info_by_id(item['recipe_id']))!=item['profile']['source_recipe_revision']:
                raise ValueError('Recipe changed before activation')
            old=connection.execute(text('SELECT row_to_json(p) FROM "nutrients-recipe-profiles" p WHERE recipe_id=:recipe AND nutrition_source=:source FOR UPDATE'),params).scalar()
            if old is None:raise ValueError('Missing production mirror')
            inputs={'source_context_repair_of_run_id':item['old_run_id'],
                'quantity_input_hash':item['profile']['quantity_input_hash'],'source_recipe_revision':item['profile']['source_recipe_revision']}
            version=item['profile']['mechanism_version']
            new_run_id=store.digest([item['recipe_id'],item['nutrition_source'],item['release_id'],version,store.digest(inputs),item['profile']])
            report['entries'].append({**item,'inputs':inputs,'new_run_id':new_run_id,'previous_mirror':old})
        report.update(deployment=dict(deployment),reference_fingerprint=before)
        backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
        for item in report['entries']:
            run=store.insert_run(connection,item['recipe_id'],item['nutrition_source'],item['release_id'],
                item['profile']['mechanism_version'],item['inputs'],item['profile'],activate=True)
            if run!=item['new_run_id']:raise ValueError('New run digest differs')
            _mirror(connection,item['recipe_id'],item['nutrition_source'],run)
        for recipe_id in sorted({item['recipe_id'] for item in entries}):
            store.enqueue_projection(connection,recipe_id)
        if before!=_reference_fingerprint(connection):raise ValueError('Reference rows changed')
    report['status']='activated'
    backup.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    return report


def rollback(backup: Path) -> None:
    report=json.loads(backup.read_text())
    with get_engine().begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('matching_production_promotion',0))"))
        for item in report['entries']:
            params={'recipe':item['recipe_id'],'source':item['nutrition_source']}
            current=connection.execute(text('SELECT run_id FROM matching_active_profiles WHERE recipe_id=:recipe AND nutrition_source=:source FOR UPDATE'),params).scalar()
            if current!=item['new_run_id']:raise ValueError('Profile advanced since repair; cannot overwrite it')
            connection.execute(text('UPDATE matching_active_profiles SET run_id=:old WHERE recipe_id=:recipe AND nutrition_source=:source'),
                {**params,'old':item['old_run_id']})
            assignments=','.join('"'+field+'"=old."'+field+'"' for field in FIELDS)
            connection.execute(text('UPDATE "nutrients-recipe-profiles" p SET '+assignments+'''
                FROM json_populate_record(NULL::"nutrients-recipe-profiles",CAST(:backup AS json)) old
                WHERE p.recipe_id=:recipe AND p.nutrition_source=:source'''),
                {**params,'backup':json.dumps(item['previous_mirror'])})
        for recipe_id in sorted({item['recipe_id'] for item in report['entries']}):store.enqueue_projection(connection,recipe_id)
    report['status']='rolled_back'
    backup.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,action='append')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    if args.rollback:rollback(args.output)
    else:
        entries=prepare([json.loads(path.read_text()) for path in args.report or []])
        if args.apply:
            result=activate(entries,args.output)
            print(json.dumps({'status':result['status'],'profiles':len(result['entries'])}))
        else:
            args.output.write_text(json.dumps({'status':'validated_preflight','entries':entries},indent=2,ensure_ascii=False)+'\n')
            print(json.dumps({'status':'validated_preflight','profiles':len(entries)}))
