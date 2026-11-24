"""Recalculate stored recipe profiles into resumable, versioned matching runs."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from sqlalchemy import text
from recipe_wrangler.matching.runtime import engine,VERSION
from recipe_wrangler.matching.quantities import source_recipe_index,recipe_inputs
from recipe_wrangler.matching.source_context import graph_source_lines
from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.utils.nutrition_postgres import get_engine
from scripts.composition.repair_original_ingredient_context import recover_missing_context


# Graph originals for these sources came from a stale, fragmented scrape ("1 to" / "2 Tablespoons lime juice").
ARCHIVED_SOURCES={'FoodHero':Path('data/recipes/foodhero/foodhero_recipes.json')}


def archived_source_lines(rows,archives=ARCHIVED_SOURCES):
    """Complete archived lines for rows whose title resolves to exactly one archived recipe."""
    normalize=lambda value:' '.join(str(value or '').casefold().split())
    out={}
    for source,path in archives.items():
        by_title={}
        for recipe in json.loads(Path(path).read_text()).values():
            by_title.setdefault(normalize(recipe.get('title')),[]).append(recipe.get('ingredients') or [])
        for row in rows:
            matches=by_title.get(normalize(row['title']),[]) if row['source']==source else []
            lines=[line for line in (matches[0] if len(matches)==1 else []) if isinstance(line,str) and line.strip()]
            if lines:out[row['recipe_id']]=lines
    return out


def bind_source_lines(names,originals,frozen,lines,weights=None):
    """Fill blank source text from archived lines one-to-one; frozen evidence wins but its blanks are filled too.

    Frozen evidence whose saved names or weights predate later corrections is stale: keep its text, drop its quantities.
    """
    if frozen is not None and weights is not None and _stale(frozen,names,weights):
        originals=[old or (o.get('original_text') or '') for old,o in zip(originals,frozen['occurrences'],strict=True)]
        frozen=None
    if frozen is None:return recover_missing_context(names,originals,lines)[0],None
    frozen=copy.deepcopy(frozen);occurrences=frozen['occurrences']
    filled,_=recover_missing_context([o['parsed_name'] for o in occurrences],
        [o.get('original_text') or '' for o in occurrences],lines)
    for occurrence,line in zip(occurrences,filled,strict=True):occurrence['original_text']=line
    return originals,frozen


def _stale(frozen,names,weights):
    # Saved names or weights corrected after the evidence was frozen (e.g. "Pepper" -> "capsicum pepper").
    occurrences=frozen['occurrences']
    return len(occurrences)==len(names) and any((o.get('baseline_parsed_name') or o['parsed_name'])!=n
        or o['saved_weight_g']!=w for o,n,w in zip(occurrences,names,weights))


def backfill(release_id,limit=None,activate=False,source_evidence=None,shard=0,shards=1,recipe_ids=None,sources=None,serves=None):
    with get_engine().connect() as lock:
        # One lock per shard: shards split recipes, so they never write the same profile.
        key='matching_backfill:'+release_id+('' if shards==1 else f':{shard}/{shards}')
        acquired=lock.execute(text('SELECT pg_try_advisory_lock(hashtextextended(:key,0))'),{'key':key}).scalar()
        if not acquired:raise RuntimeError('Another backfill for this release is running')
        try:return _backfill(release_id,limit,activate,source_evidence,shard,shards,recipe_ids,sources,serves)
        finally:lock.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key,0))'),{'key':key})


FULL_ROW='''SELECT recipe_id,title,source,nutrition_source,
            total_nutrients,total_nutrients_per_serving,nutri_score,nutri_score_breakdown,
            nutrition_profiling_details,nutrition_profiling_debug,profiling_quality,trace,pipeline_version
            FROM "nutrients-recipe-profiles"'''


def _backfill(release_id,limit=None,activate=False,source_evidence=None,shard=0,shards=1,recipe_ids=None,sources=None,serves_override=None):
    matcher=engine(release_id)
    with get_engine().connect() as c:
        # Keys only up front; each full row (with large debug/trace fields) is read when processed.
        rows=[dict(r) for r in c.execute(text('''SELECT recipe_id,title,source,nutrition_source
            FROM "nutrients-recipe-profiles"
            WHERE nutrition_source = ANY(:sources)
            AND (CAST(:ids AS text[]) IS NULL OR recipe_id = ANY(:ids))
            AND mod(abs(hashtextextended(recipe_id,0)),:shards)=:shard
            ORDER BY recipe_id,nutrition_source'''),{'shards':shards,'shard':shard,'ids':recipe_ids,
            'sources':sources or ['eu','irish','hungarian','slovenian']}).mappings()]
        done={(r['recipe_id'],r['nutrition_source']):(r['quantity_input_hash'],r['originals']) for r in c.execute(text('''
            SELECT DISTINCT ON(recipe_id,nutrition_source) recipe_id,nutrition_source,
                profile->>'quantity_input_hash' AS quantity_input_hash,
                (SELECT jsonb_agg(COALESCE(d->>'original_ingredient_text','') ORDER BY (d->>'position')::int)
                 FROM jsonb_array_elements(profile->'details') d) AS originals FROM matching_profile_runs
            WHERE release_id=:release AND mechanism_version=:version
            ORDER BY recipe_id,nutrition_source,created_at DESC,run_id DESC'''),
            {'release':release_id,'version':VERSION}).mappings()}
    frozen=Path(source_evidence) if source_evidence else Path('data/nutrition/matching/reference/cross_dataset_reference_2026-10-27_v10_final/recipes.json')
    if source_evidence and not frozen.is_file():raise FileNotFoundError(frozen)
    evidence={}
    if frozen.exists():
        evidence=source_recipe_index(json.loads(frozen.read_text()))
    # Saved profiles lost most source lines; bind the graph originals one-to-one, never by position.
    source_lines={**graph_source_lines(sorted({row['recipe_id'] for row in rows})),**archived_source_lines(rows)}
    failures=[];processed=0
    output=Path('analysis')/('matching_backfill_'+release_id);output.mkdir(exist_ok=True)
    name='status' if shards==1 else f'status_{shard}_of_{shards}'
    def checkpoint(status):
        temporary=output/(name+'.tmp')
        temporary.write_text(json.dumps({'status':status,'processed_this_run':processed,
            'previous_runs':len(done),'eligible_profiles':len(rows),'activated':activate,
            'failures':failures},indent=2))
        temporary.replace(output/(name+'.json'))
    checkpoint('running')
    for row in rows:
        key=(row['recipe_id'],row['nutrition_source'])
        if limit is not None and processed>=limit:break
        try:
            with get_engine().connect() as c:
                row=dict(c.execute(text(FULL_ROW+' WHERE recipe_id=:recipe AND nutrition_source=:source'),
                    {'recipe':key[0],'source':key[1]}).mappings().one())
            details=row['nutrition_profiling_details'] or []
            if not details:raise ValueError('No saved ingredient details')
            names=[d.get('name') or d.get('ingredient') for d in details]
            weights=[d.get('weight_g') for d in details]
            if any(not name for name in names):raise ValueError('Missing saved ingredient identity')
            originals=[d.get('original_ingredient_text') or '' for d in details]
            originals,frozen=bind_source_lines(names,originals,evidence.get(row['recipe_id']),
                source_lines.get(row['recipe_id'],[]),weights=weights)
            inputs=recipe_inputs(names,weights,originals,frozen)
            if done.get(key)==(inputs['quantity_input_hash'],inputs['originals']) and key[0] not in (serves_override or {}):continue
            names=inputs['names'];originals=inputs['originals']
            quantities=[q['quantity_evidence'] for q in inputs['quantities']]
            quality=row['profiling_quality'] or {};trace=row['trace'] or {}
            serves=(serves_override or {}).get(row['recipe_id']) or quality.get('serves') or trace.get('serves')
            # Snapshot old data before any active-run change; reference rows are excluded.
            store.save_run(row['recipe_id'],row['nutrition_source'],None,'legacy_snapshot_complete_v2',
                {'profile':row},row)
            result=matcher.calculate(title=row['title'],ingredient_names=names,weights=weights,
                source=row['nutrition_source'],serves=serves,original_texts=originals,
                quantity_evidence=quantities)
            result['recipe_source']=row['source']
            run=store.save_run(row['recipe_id'],row['nutrition_source'],release_id,VERSION,
                {**inputs,'serves':serves},result,activate=activate)
            processed+=1
            if processed%10==0:
                checkpoint('running')
                print('Processed',processed,'/',len(rows)-len(done),'latest',run,flush=True)
        except Exception as exc:
            failures.append({'recipe_id':row['recipe_id'],'source':row['nutrition_source'],
                'error':type(exc).__name__+': '+str(exc)})
            print('Failed',failures[-1],flush=True)
            checkpoint('running')
    checkpoint('failed' if failures else 'limited' if limit is not None else 'completed')
    print('Finished',processed,'failures',len(failures),flush=True)
    if failures:raise RuntimeError('Backfill has failures; see status.json')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    p.add_argument('--limit',type=int);p.add_argument('--activate',action='store_true')
    p.add_argument('--source-evidence',type=Path)
    p.add_argument('--recipe-ids',type=Path,help='File with one recipe_id per line; default all')
    p.add_argument('--sources',help='Comma-separated nutrition sources; default eu,irish,hungarian,slovenian')
    p.add_argument('--serves',action='append',default=[],help='RECIPE_ID=SERVINGS override (repeatable)')
    p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=1)
    a=p.parse_args()
    if not 0<=a.shard<a.shards:p.error('--shard must be in [0, --shards)')
    backfill(a.release_id,a.limit,a.activate,a.source_evidence,a.shard,a.shards,
        a.recipe_ids.read_text().split() if a.recipe_ids else None,a.sources.split(',') if a.sources else None,{k:float(v) for k,v in (x.split('=') for x in a.serves)})
