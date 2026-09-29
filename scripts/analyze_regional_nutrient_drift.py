"""Read-only paired audit of stored regional profiles; no profiling or database writes.

Run: uv run python scripts/analyze_regional_nutrient_drift.py [--refresh]
The local snapshot contains operational data and belongs under data/, not git.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

REGIONS = ('eu', 'irish', 'hungarian', 'slovenian')
NUTRIENTS = ('energy_kcal', 'protein_g', 'carbohydrate_g', 'fat_g', 'sugar_g',
             'saturated_fat_g', 'fibre_g', 'sodium_mg')
# Audit triage thresholds, not clinical or regulatory cutoffs.
FLOORS = dict(zip(NUTRIENTS, (50, 5, 5, 5, 5, 2, 2, 200)))
OUT = Path(__file__).resolve().parents[1] / 'data/analysis/regional_nutrient_drift'


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def serves(row):
    ratios = []
    for k in NUTRIENTS:
        a = number((row.get('total_nutrients') or {}).get(k))
        b = number((row.get('total_nutrients_per_serving') or {}).get(k))
        if a is not None and b is not None and a > 0 and b > 0:
            ratios.append(a / b)
    return float(np.median(ratios)) if ratios else None


def ingredients(row):
    grouped = defaultdict(float)
    for d in row.get('nutrition_profiling_details') or []:
        name = str(d.get('ingredient') or d.get('name') or '').strip().casefold()
        grouped[name] += number(d.get('weight_g')) or 0
    return dict(grouped)


def same_weights(a, b):
    return a.keys() == b.keys() and all(math.isclose(a[k], b[k], abs_tol=0.01, rel_tol=1e-6) for k in a)


def write_csv(name, rows):
    if not rows:
        return
    with (OUT / name).open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def refresh():
    from sqlalchemy import text
    from recipe_wrangler.utils.nutrition_postgres import get_connection, _get_config
    cfg = _get_config()
    table = '"' + str(cfg['schema']).replace('"', '""') + '"."' + str(cfg['profiles_table']).replace('"', '""') + '"'
    with get_connection() as conn, (OUT / 'profiles.jsonl.tmp').open('w') as f:
        conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        rows = conn.execution_options(stream_results=True).execute(text(f'''
            SELECT recipe_id,title,source,nutrition_source,pipeline_version,computed_at,
                total_nutrients,total_nutrients_per_serving,nutrition_profiling_details,
                trace->'serves' AS trace_serves,nutri_score,profiling_quality
            FROM {table} WHERE nutrition_source IN ('eu','irish','hungarian','slovenian')
            ORDER BY recipe_id,nutrition_source''')).mappings()
        for row in rows:
            f.write(json.dumps(dict(row), default=str) + '\n')
    (OUT / 'profiles.jsonl.tmp').replace(OUT / 'profiles.jsonl')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.refresh or not (OUT / 'profiles.jsonl').exists():
        refresh()
    profiles = defaultdict(dict)
    quality, soup_rows, ingredient_rows = [], [], []
    source_counts = Counter()
    for line in (OUT / 'profiles.jsonl').open():
        r = json.loads(line)
        region = r['nutrition_source']
        rid = r['recipe_id']
        assert region not in profiles[rid], f'Duplicate profile: {rid}/{region}'
        r['_serves'] = serves(r)
        r['_weights'] = ingredients(r)
        profiles[rid][region] = r
        ds = r.get('nutrition_profiling_details') or []
        weight = sum(number(d.get('weight_g')) or 0 for d in ds)
        matched_weight = sum((number(d.get('weight_g')) or 0) for d in ds
                             if d.get('matched_nutritional_ingredient') and not d.get('rejected_low_confidence'))
        errors = []
        for k, dk in [('protein_g', 'protein_g'), ('carbohydrate_g', 'carbs_g'), ('fat_g', 'fat_g'), ('energy_kcal', 'energy_kcal')]:
            total = number((r.get('total_nutrients') or {}).get(k))
            if ds and all(number(d.get(dk)) is not None for d in ds) and total is not None:
                summed = sum(float(d[dk]) for d in ds)
                if not math.isclose(summed, total, abs_tol=0.05, rel_tol=0.001):
                    errors.append(k)
        q = dict(recipe_id=rid, title=r['title'], source=r['source'], region=region,
                 pipeline_version=r['pipeline_version'], computed_at=r['computed_at'], serves=r['_serves'],
                 ingredient_rows=len(ds), distinct_names=len(r['_weights']), weight_g=weight,
                 matched_weight_fraction=matched_weight/weight if weight > 0 else None,
                 reconciliation_failures=';'.join(errors), quality_present=bool(r.get('profiling_quality')))
        quality.append(q)
        if 'vegetable soup' in str(r['title']).lower():
            soup_rows.append({**q, **{k:(r.get('total_nutrients_per_serving') or {}).get(k) for k in NUTRIENTS}})
        for d in ds:
            source_counts[(region, str(d.get('source_nutrition') or d.get('source')), bool(d.get('rejected_low_confidence')))] += 1
    complete = {rid: rs for rid, rs in profiles.items() if all(k in rs for k in REGIONS)}
    pairs, controls, flags = [], [], []
    for a, b in combinations(REGIONS, 2):
        cells = defaultdict(list)
        control_counts = Counter()
        for rid, rs in complete.items():
            x, y = rs[a], rs[b]
            sx, sy = x['_serves'], y['_serves']
            same_s = sx is not None and sy is not None and math.isclose(sx, sy, rel_tol=1e-6, abs_tol=1e-6)
            same_w = same_weights(x['_weights'], y['_weights'])
            same_v = x['pipeline_version'] == y['pipeline_version']
            control_counts['recipes'] += 1
            control_counts['same_serves'] += same_s
            control_counts['same_ingredient_names_and_weights'] += same_w
            control_counts['same_inputs'] += same_s and same_w
            control_counts['same_inputs_and_pipeline_label'] += same_s and same_w and same_v
            for k in NUTRIENTS:
                vx = number((x.get('total_nutrients_per_serving') or {}).get(k))
                vy = number((y.get('total_nutrients_per_serving') or {}).get(k))
                if vx is None or vy is None or min(vx, vy) < 0:
                    continue
                delta = vy - vx
                pct = 100 * delta / vx if vx > 0 else None
                ratio = max(vx, vy)/min(vx, vy) if min(vx, vy) > 0 else None
                large = abs(delta) >= FLOORS[k] and (min(vx, vy) == 0 or ratio >= 2)
                item = (vx, vy, delta, pct, large)
                cells[(k, 'all')].append(item)
                if same_s and same_w:
                    cells[(k, 'same_inputs')].append(item)
                if same_s and same_w and same_v:
                    cells[(k, 'same_inputs_and_pipeline_label')].append(item)
                if large:
                    flags.append(dict(recipe_id=rid,title=x['title'],source=rs['eu']['source'],region_a=a,region_b=b,
                        nutrient=k,value_a=vx,value_b=vy,delta=delta,ratio=ratio,
                        same_serves=same_s,same_weights=same_w,same_pipeline_label=same_v))
        controls.append(dict(region_a=a,region_b=b,**control_counts))
        for (k, cohort), values in cells.items():
            arr = np.array([[v[0],v[1],v[2]] for v in values])
            percents = [v[3] for v in values if v[3] is not None]
            pairs.append(dict(region_a=a,region_b=b,nutrient=k,cohort=cohort,n=len(values),
                median_a=float(np.median(arr[:,0])),median_b=float(np.median(arr[:,1])),
                median_signed_delta=float(np.median(arr[:,2])),median_abs_delta=float(np.median(abs(arr[:,2]))),
                p95_abs_delta=float(np.percentile(abs(arr[:,2]),95)),
                median_signed_pct=float(np.median(percents)) if percents else None,
                median_abs_pct=float(np.median(np.abs(percents))) if percents else None,
                pct_denominator_n=len(percents),zero_a=int(sum(arr[:,0]==0)),zero_b=int(sum(arr[:,1]==0)),
                large_drift_count=sum(v[4] for v in values)))
    # Ingredient attribution for all soup profiles and the 20 largest absolute cases per nutrient.
    selected = {r['recipe_id'] for r in soup_rows}
    for k in NUTRIENTS:
        selected.update(r['recipe_id'] for r in sorted((f for f in flags if f['nutrient']==k), key=lambda f:abs(f['delta']), reverse=True)[:20])
    for rid in sorted(selected):
        for region, r in complete[rid].items():
            for i,d in enumerate(r.get('nutrition_profiling_details') or []):
                ingredient_rows.append(dict(recipe_id=rid,title=r['title'],region=region,position=i,
                    ingredient=d.get('ingredient') or d.get('name'),weight_g=d.get('weight_g'),
                    matched=d.get('matched_nutritional_ingredient'),composition_source=d.get('source_nutrition') or d.get('source'),
                    food_id=d.get('canonical_food_id'),confidence=d.get('match_confidence'),rejected=d.get('rejected_low_confidence'),
                    **{k: (number(d.get('carbs_g' if k=='carbohydrate_g' else k))/r['_serves']
                           if number(d.get('carbs_g' if k=='carbohydrate_g' else k)) is not None and r['_serves'] else None) for k in NUTRIENTS}))
    source_strata = []
    for source, n in Counter(rs['eu']['source'] or 'unknown' for rs in complete.values()).items():
        subset = [f for f in flags if (f['source'] or 'unknown') == source]
        source_strata.append(dict(source=source, recipes=n,
            flagged_recipes=len({f['recipe_id'] for f in subset}),
            energy_flagged_recipes=len({f['recipe_id'] for f in subset if f['nutrient'] == 'energy_kcal'})))
    write_csv('source_strata.csv', source_strata)
    write_csv('paired_summary.csv', pairs)
    write_csv('pair_controls.csv', controls)
    write_csv('edge_cases.csv', sorted(flags,key=lambda f:(f['nutrient'],-abs(f['delta']))))
    write_csv('profile_quality.csv', quality)
    write_csv('vegetable_soups.csv', soup_rows)
    write_csv('edge_case_ingredients.csv', ingredient_rows)
    write_csv('composition_source_usage.csv',[dict(region=k[0],composition_source=k[1],rejected=k[2],ingredient_rows=v) for k,v in sorted(source_counts.items())])
    summary = dict(analyzed_at=datetime.now(timezone.utc).isoformat(),snapshot_mtime=datetime.fromtimestamp((OUT/'profiles.jsonl').stat().st_mtime,timezone.utc).isoformat(),
        recipes=len(profiles),complete_four_region_recipes=len(complete),profiles=len(quality),
        flagged_recipes=len({f['recipe_id'] for f in flags}),flagged_pair_nutrients=len(flags),thresholds=FLOORS,
        profile_sources=dict(Counter(r['source'] or 'unknown' for r in quality if r['region']=='eu')),
        quality_by_region={region:dict(profiles=sum(q['region']==region for q in quality),
            reconciliation_failures=sum(q['region']==region and bool(q['reconciliation_failures']) for q in quality),
            repeated_ingredient_names=sum(q['region']==region and q['ingredient_rows']>q['distinct_names'] for q in quality),
            coverage_below_80pct=sum(q['region']==region and q['matched_weight_fraction'] is not None and q['matched_weight_fraction']<.8 for q in quality),
            quality_present=sum(q['region']==region and q['quality_present'] for q in quality)) for region in REGIONS})
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(12,5),layout='constrained')
    for ax,field,title in [(axes[0],'median_signed_pct','Median paired change vs EU (%)'),(axes[1],'large_drift_count','Recipes with ≥2× drift + absolute floor (%)')]:
        matrix=[]
        for k in NUTRIENTS:
            vals=[]
            for region in REGIONS[1:]:
                p=next(p for p in pairs if p['region_a']=='eu' and p['region_b']==region and p['nutrient']==k and p['cohort']=='all')
                vals.append(p[field] if field=='median_signed_pct' else 100*p[field]/p['n'])
            matrix.append(vals)
        matrix=np.array(matrix)
        im=ax.imshow(matrix,cmap='RdBu_r' if field=='median_signed_pct' else 'YlOrRd',aspect='auto',vmin=-100 if field=='median_signed_pct' else 0,vmax=100)
        for i in range(len(NUTRIENTS)):
            for j in range(3): ax.text(j,i,f'{matrix[i,j]:.1f}',ha='center',va='center',fontsize=9)
        ax.set_xticks(range(3),['Ireland','Hungary','Slovenia'])
        ax.set_yticks(range(8),[k.replace('_',' ') for k in NUTRIENTS]);ax.set_title(title,fontsize=11)
    fig.suptitle(f'Stored regional nutrition · {len(complete):,} paired recipes\nEU is a comparison baseline, not ground truth',fontsize=13)
    fig.savefig(OUT/'regional_drift.png',dpi=180)
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
