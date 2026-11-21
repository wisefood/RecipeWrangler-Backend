#!/usr/bin/env python3
"""Build and load recipe_adaptations rows (mode=nutrition) from the frozen Living Lab results.

Reads evaluation/adaptation/results/nutrition_results.json and nutrition_llm_rewrites.json, plus the saved inputs in
analysis/nutrition-adaptation-2026-11-11/inputs for the original lines and steps. One row per recipe, in its native region
(IE, HU or SI; no EU row is computed). Rewritten recipes keep the LLM text; the one recipe without a rewrite and every
enriched recipe get a code-written card (no model call here).

Default is a DRY RUN: rows are validated and summarised. `--apply` upserts them in ONE transaction (idempotent).
Usage: load_adaptations.py [--apply]
"""
import argparse, gzip, json, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from recipe_wrangler.services.adaptation.nutrition_adaptation_llm import STEP_NUMBER, flat_steps

RESULTS = ROOT / 'evaluation' / 'adaptation' / 'results'
INPUTS = ROOT / 'analysis' / 'nutrition-adaptation-2026-11-11' / 'inputs'
VERSION = '2026-11-20'
CODE = {'irish': 'IE', 'hungarian': 'HU', 'slovenian': 'SI'}
WHY = {
    'already_healthy': 'The recipe already scores A and no swap, cut or addition improves it by a visible amount without making another nutrient worse.',
    'gain_too_small': 'The best option improves the recipe by too little to be worth suggesting.',
    'main_food_defines_the_dish': 'The food that would need to change is what defines the dish, so no swap is suggested.',
    'no_healthier_option': 'No healthier option of the same kind passed the checks.',
}
METRIC = {'add_fruit_veg': 'fibre_g', 'add_fibre': 'fibre_g', 'add_protein': 'protein_g'}


def build_rows():
    results = json.load(open(RESULTS / 'nutrition_results.json'))
    rewrites = json.load(open(RESULTS / 'nutrition_llm_rewrites.json'))
    rows = []
    for rid, r in results.items():
        base = dict(recipe_id=rid, mode='nutrition', region=CODE[r['region']], version=VERSION, reason_code=None,
                    original=None, adapted=None, swap=None, benefit=None,
                    provenance={'source': 'Nutri-Score (original algorithm), regional composition table then EU', 'native_region': CODE[r['region']]})
        if r['outcome'] == 'no_sensible_adaptation':
            rows.append(dict(base, status='no_suggestion', reason_code=r['reason_code'], provenance=dict(base['provenance'], reason=WHY[r['reason_code']])))
            continue
        cap = json.load(gzip.open(INPUTS / f'{rid}.json.gz', 'rt'))
        items = sorted(cap['snapshot']['ingredients'], key=lambda i: i['position'])
        lines = [i['original_text'] or i['name'] for i in items]
        steps = [STEP_NUMBER.sub('', s) for s in flat_steps(cap['source_recipe'].get('instructions'))]
        original = dict(title=r['title'], lines=lines, steps=steps)
        metric = lambda name: dict(name=name, unit='per serving', before=r['before_per_serving'][name], after=r['after_per_serving'][name],
                                   change=round(r['after_per_serving'][name] - r['before_per_serving'][name], 3))
        if r['outcome'] == 'adapted':
            llm = rewrites.get(rid)
            if llm:
                new = {l['position']: l['new'] for l in llm['ingredient_lines']}
                adapted = dict(title=llm['title'], lines=[new.get(i['position'], t) for i, t in zip(items, lines)], steps=llm['instructions'], changed_positions=sorted(new))
                benefit, text = llm['benefit'], 'llm_rewrite'
            else:
                new = {c['position']: f"{c['source_line']}  →  use {c['candidate_label']}" for c in r['changes']}
                adapted = dict(title=r['title'], lines=[new.get(i['position'], t) for i, t in zip(items, lines)], steps=steps, changed_positions=sorted(new))
                benefit = dict(facet='nutrition', tip='; '.join(f"{c['source_line']}: use {c['candidate_label']}" for c in r['changes']), tip_source='code', metric=metric('fibre_g'), swap=[], notes=[])
                text = 'code_card'
            rows.append(dict(base, status='adapted', original=original, adapted=adapted, swap=r['changes'], benefit=benefit,
                             provenance=dict(base['provenance'], text=text, grade_before=r['baseline_grade'], grade_after=r['grade_after'])))
        else:
            a = r['addition']
            serve = f"Serve with: {a['candidate_label'].lower()} ({a['grams_per_serving']} g per serving)"
            tip = a['reason'][0].upper() + a['reason'][1:] + '.'
            rows.append(dict(base, status='adapted', original=original,
                             adapted=dict(title=r['title'], lines=lines + [serve], steps=steps + [serve + '.'], changed_positions=[len(lines)]),
                             swap=[a], benefit=dict(facet='nutrition', tip=tip, tip_source='code', metric=metric(METRIC[a['category']]), swap=[], notes=[]),
                             provenance=dict(base['provenance'], text='code_card', enrichment=True, grade_before=r['baseline_grade'])))
    return rows


def apply(rows):
    sys.path.insert(0, str(ROOT / 'scripts' / 'sustainability'))
    from load_adaptations import apply as upsert
    upsert(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    rows = build_rows()
    assert len({(r['recipe_id'], r['mode'], r['region']) for r in rows}) == len(rows), 'duplicate keys'
    assert all(all(r[k] for k in ('original', 'adapted', 'swap', 'benefit')) for r in rows if r['status'] == 'adapted')
    print(len(rows), 'rows,', dict(sorted(Counter((r['region'], r['status']) for r in rows).items())))
    print('text', dict(Counter(r['provenance'].get('text') for r in rows if r['status'] == 'adapted')))
    if args.apply:
        apply(rows)
        print('upserted into recipe_adaptations')
    else:
        print('dry run: database not touched (use --apply)')


if __name__ == '__main__':
    main()
