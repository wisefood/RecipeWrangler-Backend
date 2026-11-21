"""Replay frozen tagging cases without a database, local model, or paid annotator.

Keep unchanged silver labels and separate source-reviewed cells in the output.
The baseline source directory contains pre-change food_ontology.py and
neo4j_recipes.py snapshots. No files in the original evaluation are overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys

from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
from recipe_wrangler.utils.food_ontology import ALLERGEN_ONTOLOGY_MAPPINGS


def evidence_states(rows: list[dict]) -> dict[str, str]:
    result = {}
    for row in rows:
        presence = row.get('presence', 'contains')
        label = 'present' if presence in {'contains', 'present'} else 'possible'
        allergen = row['allergen']
        if result.get(allergen) != 'present':
            result[allergen] = label
    return result


def comparison(truth: dict[str, str], predicted: dict[str, str]) -> dict:
    return dict(Counter(
        f'{label}->{predicted.get(allergen, "not_flagged")}'
        for allergen, label in truth.items()
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path('evaluation/allergens_diets')
    adjudications = json.loads((args.output/'adjudications.json').read_text())
    reviews = {(r['set'], r['recipe_id'], r['item']): r for r in adjudications if r['kind'] == 'allergen'}
    spec = importlib.util.spec_from_file_location('_tagging_baseline_ontology', args.baseline_source/'food_ontology.py')
    old_ontology = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old_ontology
    spec.loader.exec_module(old_ontology)
    old = runpy.run_path(str(args.baseline_source/'neo4j_recipes.py'))
    baseline = old['detect_allergen_evidence_from_names']
    baseline.__globals__['_ALLERGEN_KEYWORDS'] = {
        a: list(dict.fromkeys(rule['keywords'] + getattr(old_ontology, 'ALLERGEN_POSSIBLE_KEYWORDS', {}).get(a, [])))
        for a, rule in old_ontology.ALLERGEN_DETECTION_RULES.items()
    }
    baseline.__globals__['CLASSIFICATION_VERSION'] = old_ontology.CLASSIFICATION_VERSION
    summaries, predictions = {}, {}
    source_files = [args.baseline_source/'food_ontology.py', args.baseline_source/'neo4j_recipes.py']
    for name, suffix in [('representative', 'representative'), ('hard_cases', 'hard')]:
        inputs = root/f'data/frozen-sets/{name}.json'
        annotations = root/f'results/tagging-2026-11-03/annotations_{suffix}.jsonl'
        source_files.extend([inputs, annotations])
        records = {r['recipe_id']: r for r in json.loads(inputs.read_text()) if r.get('recipe_id')}
        labels = {r['recipe_id']: r['annotation'] for line in annotations.read_text().splitlines()
                  if 'annotation' in (r := json.loads(line))}
        assert records.keys() == labels.keys(), 'Frozen recipes/annotations do not align'
        totals = {view: Counter() for view in ['baseline', 'candidate', 'frozen_live_plus_candidate']}
        reviewed = {view: Counter() for view in ['baseline', 'candidate', 'frozen_live_plus_candidate']}
        per_allergen = {view: {a: Counter() for a in ALLERGEN_ONTOLOGY_MAPPINGS} for view in totals}
        cases = []
        for rid, rec in records.items():
            old_rows = baseline(rec['original_lines'])
            new_rows = detect_allergen_evidence_from_names(rec['original_lines'])
            old_states, new_states = evidence_states(old_rows), evidence_states(new_rows)
            live_union = {**new_states, **{a: 'present' for a in rec['system_state'].get('allergens', [])}}
            truth = {a: 'absent' for a in ALLERGEN_ONTOLOGY_MAPPINGS}
            truth.update(evidence_states([
                {'allergen': row['allergen'], 'presence': row['label']}
                for row in labels[rid]['allergens'] if row['label'] in {'present', 'possible'}
            ]))
            reviewed_truth = {a: reviews[(name, rid, a)]['reviewed_label']
                              for a in truth if (name, rid, a) in reviews}
            for view, states in [('baseline', old_states), ('candidate', new_states), ('frozen_live_plus_candidate', live_union)]:
                totals[view].update(comparison(truth, states))
                reviewed[view].update(comparison(reviewed_truth, states))
                for a, label in truth.items():
                    per_allergen[view][a][f'{label}->{states.get(a, "not_flagged")}'] += 1
            cases.append({'recipe_id': rid, 'title': rec['title'], 'original_lines': rec['original_lines'],
                          'baseline_evidence': old_rows, 'candidate_evidence': new_rows,
                          'frozen_silver_labels': truth, 'source_reviewed_cells': reviewed_truth,
                          'frozen_live_plus_candidate': live_union})
        summaries[name] = {'recipes': len(cases), 'unchanged_silver_confusion': totals,
                           'source_reviewed_cells_confusion': reviewed, 'per_allergen_silver': per_allergen}
        predictions[name] = cases
    source_files.extend([Path(__file__), args.output/'adjudications.json',
                         Path('src/recipe_wrangler/repositories/neo4j_recipes.py'),
                         Path('src/recipe_wrangler/utils/food_ontology.py'),
                         Path('src/recipe_wrangler/utils/consumer_suitability.py'),
                         Path('src/recipe_wrangler/utils/ingredient_context.py'),
                         Path('src/recipe_wrangler/utils/foodon_matching.py'),
                         Path('src/recipe_wrangler/utils/dietary_classification.py'),
                         Path('src/recipe_wrangler/utils/es_recipe_evidence.py'),
                         Path('src/recipe_wrangler/api/routers/recipes.py'),
                         Path('scripts/neo4j/tag_allergens.py')])
    manifest = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    (args.output/'predictions.json').write_text(json.dumps(predictions, indent=2, ensure_ascii=False)+'\n')
    (args.output/'summary.json').write_text(json.dumps(summaries, indent=2)+'\n')
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({name: {k: v for k, v in result.items() if k != 'per_allergen_silver'}
                      for name, result in summaries.items()}, indent=2))


if __name__ == '__main__':
    main()
