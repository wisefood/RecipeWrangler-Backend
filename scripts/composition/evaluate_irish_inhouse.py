"""Freeze an Irish development pack and capture/replay the production baseline.

Only capture reads local backing services. It never writes recipe/nutrition stores.
All other stages consume files; expectations are assistant-authored, provisional.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PILOT = ROOT/'analysis/irish_living_lab_hybrid_pilot_2026-10-26_name_only'
SOURCE = ROOT/'analysis/living_lab_matching_audits_2026-10-13/irish_living_lab_ingredient_matches.csv'
TITLES = [
    'Banana and oat smoothie', 'Strawberry smoothie', 'Porridge', 'Chewy apricot porridge',
    'Simple oat bread', 'Grilled Cajun salmon', 'Teriyaki salmon', 'Scrambled egg on toast',
    'Poached eggs', 'Side salad', 'Greek pasta salad', 'Egg fried rice', 'Lentil soup',
    'Chickpea and lentil curry', 'Mixed bean and rice salad', 'Tuna pasta salad',
    'One pot roast chicken and vegetables', 'Pasta with tomato and mascarpone sauce',
    'Homemade burgers', 'Leek and potato soup',
]
METRICS = ['energy_kcal', 'protein_g', 'carbs_g', 'fat_g', 'sugar_g',
           'saturated_fat_g', 'sodium_mg', 'fibre_g']


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str, allow_nan=False)+'\n')


def prepare(output: Path) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw_paths = sorted((ROOT/'data/recipes/safefood_web').glob('*recipes.json'))
    raw = {r['name']: (r, str(p.relative_to(ROOT))) for p in raw_paths for r in json.loads(p.read_text())}
    grouped = defaultdict(list)
    with SOURCE.open(encoding='utf-8-sig') as f:
        for number, row in enumerate(csv.DictReader(f), 2):
            if row['recipe_title'] in TITLES:
                grouped[row['recipe_title']].append(dict(row, source_csv_row=number))
    recipes = []
    for index, title in enumerate(TITLES, 1):
        original, raw_path = raw[title]
        rows = grouped[title]
        if not rows or len({r['recipe_id'] for r in rows}) != 1:
            raise ValueError(f'Missing or nonunique snapshot recipe: {title}')
        occurrences = []
        for position, row in enumerate(rows, 1):
            occurrences.append({'occurrence_id': f'recipe-{index:02d}-ingredient-{position:02d}',
                'position_in_saved_export': position, 'original_text': row['original_ingredient_text'],
                'parsed_name': row['ingredient_name'], 'measurement': row['measurement'],
                'saved_weight_g': float(row['weight_grams']), 'source_csv_row': row['source_csv_row']})
        recipes.append({'evaluation_id': f'recipe-{index:02d}', 'recipe_id': rows[0]['recipe_id'],
            'title': title, 'stratum': 'ordinary_development' if index <= 10 else 'challenge_development',
            'raw_source_file': raw_path, 'url': original['url'], 'serves': float(original['serves']),
            'original_ingredients': original['ingredients'], 'instructions': original['method'],
            'published_nutrition_diagnostic_only': original['nutrition'], 'occurrences': occurrences})
    dump(output/'pack.json', {'split': 'inhouse_development_only',
        'selection': 'Purposive 10 ordinary + 10 challenge recipes; not representative or held-out.',
        'label_status': 'Assistant expectations to be frozen before any matching fixes; not expert gold.',
        'recipes': recipes})
    wanted = {(o['parsed_name'].strip(), 'ingredient_name')
        for r in recipes for o in r['occurrences']}
    inputs = json.loads((PILOT/'inputs.json').read_text())
    ids = {i['case_id']: (i['text'], i['input_origin']) for i in inputs if (i['text'], i['input_origin']) in wanted}
    with (output/'hybrid_frozen.jsonl').open('x') as stream:
        for line in (PILOT/'predictions.jsonl').open():
            r = json.loads(line)
            if r['case_id'] in ids:
                r['input_origin'] = ids[r['case_id']][1]
                stream.write(json.dumps(r, ensure_ascii=False)+'\n')
    assert len(ids) == len(wanted)
    paths = [SOURCE, PILOT/'manifest.json', PILOT/'predictions.jsonl', *raw_paths,
             ROOT/'src/recipe_wrangler/tools/nutrition_match.py', ROOT/'src/recipe_wrangler/tools/nutritional_calculator.py',
             ROOT/'src/recipe_wrangler/tools/recipe_profiling_chain.py']
    paths += sorted((ROOT/'src/recipe_wrangler/matching').rglob('*.py'))
    pilot_manifest = json.loads((PILOT/'manifest.json').read_text())
    changed = [path for path, digest in pilot_manifest['inputs_sha256'].items()
               if path.startswith('src/recipe_wrangler/matching/') and sha(ROOT/path) != digest]
    if changed:
        raise ValueError(f'Hybrid code differs from frozen pilot: {changed}')
    dump(output/'pack_manifest.json', {'inputs_sha256': {str(p.relative_to(ROOT)): sha(p) for p in paths},
        'pack_sha256': sha(output/'pack.json'), 'hybrid_source_run': str(PILOT.relative_to(ROOT)),
        'hybrid_reuse': 'Fresh ingredient-name-only pilot results; quantities and original text excluded from matching.',
        'input_policy': 'ingredient_name_only',
        'no_training': True, 'production_writes': False})
    print('PREPARED', len(recipes), sum(len(r['occurrences']) for r in recipes), 'occurrences', flush=True)


def baseline(output: Path, capture: bool) -> None:
    output = output.resolve()
    # Local imports avoid loading service clients for file-only pack preparation.
    from recipe_wrangler.tools import nutrition_match as nm, nutritional_calculator as nc
    from recipe_wrangler.utils.get_embeddings import DEFAULT_MODEL_NAME, DEFAULT_MODEL_REVISION
    pack = json.loads((output/'pack.json').read_text())
    destination = output/('production_capture.json' if capture else 'production_replay.json')
    if destination.exists():
        raise FileExistsError(destination)
    calls_path = output/'production_dependencies.json'
    calls = {} if capture else json.loads(calls_path.read_text())
    errors = []
    def wrap(name, fn):
        def invoke(*args, **kwargs):
            key = json.dumps([name, args, kwargs], sort_keys=True)
            if capture and key not in calls:
                try:
                    calls[key] = fn(*args, **kwargs)
                except Exception as exc:
                    errors.append({'operation': name, 'error_type': type(exc).__name__})
                    raise
            if key not in calls:
                raise AssertionError(f'Uncaptured dependency: {name}')
            return copy.deepcopy(calls[key])
        return invoke
    results, controls = [], []
    with ExitStack() as stack:
        # The comparison controls matcher inputs; quantity-based disambiguation is a separate evaluation.
        stack.enter_context(patch.object(nc, '_contextual_match_name', lambda name, *_: name))
        if not capture:
            # Any uncaptured HTTP request must fail rather than invalidate offline replay.
            stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled during replay')))
            stack.enter_context(patch('sqlalchemy.engine.Engine.connect', side_effect=AssertionError('Database disabled during replay')))
        for name in ['query_irish_nutrition_candidates', 'query_eu_nutrition_candidates',
                     'get_nutrition_candidate_by_source_id', 'load_pipeline_data']:
            stack.enter_context(patch.object(nm, name, wrap('matcher.'+name, getattr(nm, name))))
        for name in ['get_irish_ingredient_nutrition', 'get_eu_ingredient_nutrition']:
            stack.enter_context(patch.object(nc, name, wrap('calculator.'+name, getattr(nc, name))))
        nm._alias_index.cache_clear()
        for recipe in pack['recipes']:
            occurrences = recipe['occurrences']
            names = [o['parsed_name'].strip() for o in occurrences]
            result = nc.nutritional_tool_vector.invoke({'title': recipe['title'], 'ingredient_names': names,
                'ingredient_identity_names': names,
                'weights': [o['saved_weight_g'] for o in occurrences],
                'measurements': [o['measurement'] for o in occurrences], 'source': 'irish',
                'min_similarity': 0.7, 'serves': recipe['serves']})
            matches = []
            for occurrence, name in zip(occurrences, names, strict=True):
                query = name
                decision = nm.best_nutrition_match(query, 'irish', 0.7, identity_name=name)
                matches.append({'occurrence_id': occurrence['occurrence_id'], 'query': query, 'decision': decision})
                text = name
                control = nm.best_nutrition_match(text, 'irish', 0.7, identity_name=text)
                controls.append({'occurrence_id': occurrence['occurrence_id'], 'query': text, 'decision': control})
            results.append({'evaluation_id': recipe['evaluation_id'], 'title': recipe['title'],
                'source': 'current production matcher/calculator with ingredient-name-only matching; saved weights separate',
                'matches': matches, 'calculation': result})
            if capture:
                dump(calls_path, calls)
                dump(output/'production_capture.partial.json', results)
            print('BASELINE', recipe['evaluation_id'], recipe['title'], flush=True)
    if errors:
        raise RuntimeError(f'Baseline service errors: {errors}')
    dump(destination, {'recipes': results, 'identical_name_input_control': controls})
    if capture:
        import os
        from recipe_wrangler.repositories.elasticsearch_vectors import elastic_vector_index, elastic_vector_search_mode
        dump(output/'baseline_manifest.json', {'code_sha256': {str(p.relative_to(ROOT)): sha(p)
            for p in [ROOT/'src/recipe_wrangler/tools/nutrition_match.py', ROOT/'src/recipe_wrangler/tools/nutritional_calculator.py',
                      ROOT/'src/recipe_wrangler/repositories/elasticsearch_vectors.py', ROOT/'src/recipe_wrangler/repositories/vector_matchers.py',
                      ROOT/'src/recipe_wrangler/tools/recipe_profiling_chain.py', ROOT/'src/recipe_wrangler/utils/get_embeddings.py']},
            'embedding_model': DEFAULT_MODEL_NAME, 'embedding_revision_setting': DEFAULT_MODEL_REVISION,
            'index': elastic_vector_index(), 'vector_mode': elastic_vector_search_mode(),
            'weak_matches_rejected': nc.REJECT_WEAK_NUTRITION_MATCHES,
            'dependencies_sha256': sha(calls_path), 'dependency_calls': len(calls),
            'read_only_capture': True, 'external_inference_used': False,
            'input_policy': 'ingredient_name_only; source-context recovery and quantity-based query disambiguation disabled in evaluation only',
            'limitations': ['Saved parser/weight inputs, not a new live end-to-end parse.',
                'Captures relevant returned candidates and nutrient rows, not a transactional whole-index snapshot.',
                'Controlled name-only comparison; not a fresh end-to-end production parsing run.']})
    else:
        expected = json.loads((output/'production_capture.json').read_text())
        actual = json.loads(destination.read_text())
        if expected != actual:
            raise AssertionError('Production replay differs from capture')
        print('REPLAY EXACTLY MATCHES CAPTURE', flush=True)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('stage', choices=['prepare', 'capture', 'replay'])
    cli.add_argument('--output-dir', type=Path, required=True)
    args = cli.parse_args()
    if args.stage == 'prepare':
        prepare(args.output_dir)
    else:
        baseline(args.output_dir, capture=args.stage == 'capture')
