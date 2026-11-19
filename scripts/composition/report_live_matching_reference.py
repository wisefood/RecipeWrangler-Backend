"""Compare persisted live-adapter profiles with unchanged recipe references."""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine


def report(release_id,output_dir=None):
    source=Path('data/nutrition/matching/reference/cross_dataset_reference_2026-10-27_v10_final')
    recipes=json.loads((source/'recipes.json').read_text())
    excluded={(r['dataset'],r['recipe_id']) for r in json.loads((source/'reference_eligibility.json').read_text())['excluded']}
    with get_engine().connect() as c:
        profiles={(r['recipe_id'],r['nutrition_source']):dict(r) for r in c.execute(text('''
            SELECT DISTINCT ON(recipe_id,nutrition_source) recipe_id,nutrition_source,run_id,mechanism_version,
            profile->'clean_totals_per_serving' AS totals
            FROM matching_profile_runs WHERE release_id=:r
            ORDER BY recipe_id,nutrition_source,created_at DESC,run_id DESC'''),{'r':release_id}).mappings()}
    rows=[];groups=defaultdict(list);pending=0
    for recipe in recipes:
        if (recipe['dataset'],recipe['recipe_id']) in excluded:continue
        live=profiles.get((recipe['recipe_id'],recipe['region']))
        if live is None:pending+=1
        for nutrient,truth in recipe['reference'].items():
            if truth is None:continue
            key='carbohydrate_g' if nutrient=='carbs_g' else nutrient
            value=(live['totals'] or {}).get(key) if live else None
            old=recipe['saved_production'].get(nutrient)
            row={'dataset':recipe['dataset'],'recipe_id':recipe['recipe_id'],'title':recipe['title'],
                'nutrient':nutrient,'reference':truth,'stored_production':old,'new':value,
                'run_id':live['run_id'] if live else None,'calculation_available':live is not None}
            rows.append(row);groups[recipe['dataset'],nutrient].append(row)
    summaries=[]
    for (dataset,nutrient),group in groups.items():
        common=[r for r in group if r['stored_production'] is not None and r['new'] is not None]
        old_errors=[abs(r['stored_production']-r['reference']) for r in common]
        new_errors=[abs(r['new']-r['reference']) for r in common]
        summaries.append({'dataset':dataset,'nutrient':nutrient,'reference_recipes':len(group),
            'calculated_recipes':sum(r['calculation_available'] for r in group),
            'new_complete_recipes':sum(r['new'] is not None for r in group),'common_n':len(common),
            'production_mae':statistics.fmean(abs(r['stored_production']-r['reference']) for r in common) if common else None,
            'new_mae':statistics.fmean(new_errors) if common else None,
            'production_median_absolute_error':statistics.median(old_errors) if common else None,
            'new_median_absolute_error':statistics.median(new_errors) if common else None,
            'mae_change_new_minus_production':statistics.fmean(new_errors)-statistics.fmean(old_errors) if common else None,
            'mean_absolute_production_proposed_difference':statistics.fmean(abs(r['new']-r['stored_production']) for r in common) if common else None})
    folder=Path(output_dir) if output_dir else Path('analysis')/('matching_live_reference_'+release_id)
    folder.mkdir(parents=True,exist_ok=True)
    for name,data in [('recipe_deviations',rows),('summary',summaries)]:
        with (folder/(name+'.csv')).open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    lines=['# Persisted live-adapter nutrition versus recipe references','',
        f'Release: `{release_id}`. Reference recipe views still awaiting calculation: **{pending}**.',
        'This report is partial until that count is zero. RCSI overlaps SafeFood. Reference values are unchanged.',
        'MAE is per serving on identical complete recipes in both arms. New includes source-evidenced quantity corrections where available.',
        'Negative MAE change means lower deviation. Incomplete proposed totals are not scored as zero; coverage is shown explicitly. Median errors and direct production/proposed differences are also saved in summary.csv.',
        '', '| Dataset | Nutrient | Common n | Production MAE | New MAE | MAE change | New complete / reference |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in summaries:
        fmt=lambda v:'—' if v is None else f'{v:.3f}'
        lines.append(f"| {r['dataset']} | {r['nutrient']} | {r['common_n']} | {fmt(r['production_mae'])} | {fmt(r['new_mae'])} | {fmt(r['mae_change_new_minus_production'])} | {r['new_complete_recipes']}/{r['reference_recipes']} |")
    (folder/'report.md').write_text('\n'.join(lines)+'\n')
    (folder/'manifest.json').write_text(json.dumps({'release_id':release_id,'pending_reference_views':pending,
        'sha256':{str(source/name):hashlib.sha256((source/name).read_bytes()).hexdigest()
            for name in ['recipes.json','reference_eligibility.json']},
        'reference_changes':False,'selection_uses_reference':False,
        'mechanism_versions':sorted({r['mechanism_version'] for r in profiles.values()})},indent=2))
    print('Reference views awaiting calculation',pending,flush=True)
    return pending


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    p.add_argument('--output-dir');a=p.parse_args();report(a.release_id,a.output_dir)
