"""Write contextual facet proposals and coverage without changing the input catalogue."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from recipe_wrangler.matching.composition_annotation import annotate_composition, VERSION
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.langual import LangualVocabulary


def run(root: Path, input_path: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    paths = {'input': input_path,
        'dictionary': root/'data/analysis/ingredient_dictionary_2026-10-22/foodon_dictionary.json',
        'parents': root/'data/analysis/matching_vocabularies_2026-10-21/foodon_named_parents.csv',
        'vocabulary': root/'data/analysis/foodex2/17.2/facets.json',
        'native_catalogue': root/'data/raw/matching_vocabularies_2026-10-21/MTX.ecf',
        'langual': root/'data/analysis/langual/2017/terms.json'}
    annotator = HybridAnnotator(ContextualParser.from_snapshots(paths['dictionary'], paths['parents']),
                                FacetVocabulary.load(paths['vocabulary']),
                                langual=LangualVocabulary.load(paths['langual']))
    catalogue = FoodEx2Catalogue(paths['native_catalogue'])
    coverage, native_coverage, unresolved, issues = Counter(), Counter(), Counter(), Counter()
    count, unknown_heads, no_properties, unresolved_records = 0, 0, 0, 0
    with input_path.open() as source, (output/'records.jsonl').open('w') as target:
        for line in source:
            record = annotate_composition(json.loads(line), annotator, catalogue)
            target.write(json.dumps(record, ensure_ascii=False)+'\n')
            count += 1
            text = record['text_annotation']
            unknown_heads += text['main_ingredient'] is None
            present = [key for key, value in record['facets'].items()
                       if key not in {'identity', 'base_identity', 'compound'} and value not in (None, [], '')]
            coverage.update(present)
            no_properties += not present
            native_coverage.update(record['native_facets'].keys())
            unresolved_records += bool(text['unresolved'])
            unresolved.update(u['text'].casefold() for u in text['unresolved'])
            issues.update(record['issues'])
            if count % 5000 == 0:
                print(f'Annotated {count} records', flush=True)
    summary = {'records': count, 'records_without_properties': no_properties,
        'unknown_main_ingredient': unknown_heads, 'records_with_unresolved_text': unresolved_records,
        'property_record_counts': dict(coverage), 'native_property_record_counts': dict(native_coverage),
        'issues': dict(issues), 'most_common_unresolved': unresolved.most_common(100),
        'all_annotations_unreviewed': True, 'production_eligible': False}
    (output/'coverage.json').write_text(json.dumps(summary, indent=2)+'\n')
    modules = ['composition_annotation', 'contextual_parser', 'facet_vocabulary', 'hybrid',
               'composition_catalogue', 'foodex2', 'facets', 'langual', 'normalization',
               'product_identity', 'reranking']
    for name in modules:
        paths[name] = root/f'src/recipe_wrangler/matching/{name}.py'
    paths['runner'] = Path(__file__)
    manifest = {'version': VERSION, 'inputs_and_code': {
        name: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        for name, path in paths.items()}, 'limitations': [
            'No independent reference labels or validated acceptance threshold.',
            'Only mapped native codes are projected; implicit descriptors remain evidence.',
            'Free-text metadata descriptions and native LanguaL assignments are not extracted.',
            'No embedding fallback or nutrient selection is performed by this offline annotator.']}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(summary, indent=2), flush=True)
    return summary


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--input', type=Path,
                     default=Path('data/analysis/composition_catalogue_2026-10-22/records.jsonl'))
    cli.add_argument('--output-dir', type=Path, required=True)
    args = cli.parse_args()
    run(Path(__file__).resolve().parents[2], args.input, args.output_dir)
