#!/usr/bin/env python3
"""Build an offline descriptor catalogue from retained EU and regional sources."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook

from recipe_wrangler.matching.composition_catalogue import (
    CATALOGUE_VERSION, FOODEX2_BASE_MAPPINGS, PHYSICAL_STATE_MAPPINGS,
    CompositionCatalogue, annotate_record, clean_value, consolidate_sources, validate_mappings,
)
from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_efsa(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook.active.iter_rows(min_row=3, values_only=True)
        headers = next(rows)
        for number, values in enumerate(rows, start=4):
            metadata = {key: value for key, value in zip(headers, values) if key}
            label = clean_value(metadata.get('English food name'))
            if not label:
                continue
            yield {'source': {'dataset': clean_value(metadata.get('Dataset name')),
                              'release': 'EFSA EU FCDB 2026',
                              'record_id': clean_value(metadata.get('Original food code'))},
                   'label': label, 'metadata': metadata, 'source_row': number}
    finally:
        workbook.close()


def read_regional_csv(path: Path, dataset: str, metadata_columns: tuple[str, ...]):
    release = 'retained_snapshot_sha256:' + sha256(path)
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'canonical_food_id', 'Food Name'} <= set(reader.fieldnames or []):
            raise ValueError('Regional CSV lacks native ID or food name')
        for number, row in enumerate(reader, start=2):
            yield {'source': {'dataset': dataset, 'release': release, 'record_id': row['canonical_food_id']},
                   'label': row['Food Name'],
                   'metadata': {key: row.get(key) for key in metadata_columns}, 'source_row': number}


def read_cofid(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for number, row in enumerate(workbook['1.3 Proximates'].iter_rows(min_row=4, values_only=True), start=4):
            if not re.fullmatch(r'\d+-\d+', str(row[0] or '').strip()):
                continue
            yield {'source': {'dataset': 'CoFID', 'release': '2021', 'record_id': str(row[0]).strip()},
                   'label': str(row[1]).strip(),
                   'metadata': {'Description': row[2], 'Group': row[3]}, 'source_row': number}
    finally:
        workbook.close()


def read_slovenian(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    release = 'retained_snapshot_sha256:' + sha256(path)
    try:
        rows = workbook.active.iter_rows(values_only=True)
        headers = next(rows)
        for number, row in enumerate(rows, start=2):
            metadata = dict(zip(headers[:8], row[:8]))
            yield {'source': {'dataset': 'slovenian_' + str(metadata['book']), 'release': release,
                              'record_id': str(metadata['food_id'])},
                   'label': str(metadata['food_name_en']).strip(),
                   'metadata': metadata, 'source_row': number}
    finally:
        workbook.close()


def build(repo_root: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    paths = {
        'efsa': repo_root / 'data/nutrition/composition_tables/efsa_eu/foods_and_foodex2.xlsx',
        'cofid': repo_root / 'data/nutrition/composition_tables/eu_national/CoFID-UK/McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx',
        'irish': repo_root / 'data/nutrition/composition_tables/irish/Irish_Comp_Table.csv',
        'hungarian': repo_root / 'data/nutrition/composition_tables/hungarian/hungarian_comp_table.csv',
        'slovenian': repo_root / 'data/nutrition/composition_tables/slovenian/slovenian_composition_tables_unified.xlsx',
        'foodex2': repo_root / 'data/nutrition/matching/vocabularies/raw/matching_vocabularies_2026-10-21/MTX.ecf',
        'foodon_dictionary': repo_root / 'data/nutrition/matching/dictionary/ingredient_dictionary_2026-10-22/foodon_dictionary.json',
    }
    for path in paths.values():
        if not path.is_file():
            raise FileNotFoundError(path)
    catalogue = FoodEx2Catalogue(paths['foodex2'])
    validate_mappings(catalogue)
    dictionary = IngredientDictionary.load(paths['foodon_dictionary'])
    source_records = []
    readers = [read_efsa(paths['efsa']), read_cofid(paths['cofid']),
               read_regional_csv(paths['irish'], 'Irish composition table', ('Description', 'Group')),
               read_regional_csv(paths['hungarian'], 'Hungarian composition table', ('Category',)),
               read_slovenian(paths['slovenian'])]
    for reader in readers:
        source_records.extend(reader)
    consolidated = consolidate_sources(source_records)
    records = []
    coverage = defaultdict(Counter)
    datasets = Counter()
    for number, source_record in enumerate(consolidated, start=1):
        record = annotate_record(source_record, catalogue)
        def hits(text):
            return [{'term_id': hit.term_id, 'label': hit.preferred_label, 'label_type': hit.label_type}
                    for hit in dictionary.lookup(text)] if text else []
        record['foodon_lookup'] = {
            'whole_record_candidates': hits(record['label']),
            'base_food_candidates': hits(record['facets'].get('base_identity')),
            'status': 'lookup_proposals_not_validated_record_links',
        }
        records.append(record)
        datasets[record['source']['dataset']] += 1
        methods_by_field = defaultdict(set)
        for item in record['annotation_evidence']:
            methods_by_field[item['facet']].add(item['method'])
        for field, value in record['facets'].items():
            if value is None or value == []:
                coverage[field]['unresolved_or_unstated'] += 1
            elif record['review_required']:
                coverage[field]['proposed_requires_review'] += 1
            else:
                coverage[field]['proposed_available'] += 1
            for method in methods_by_field[field]:
                coverage[field][method + '_evidence'] += 1
        if number % 10000 == 0:
            print(f'Annotated {number}/{len(consolidated)} records', flush=True)
    lookup = CompositionCatalogue(records)
    report = {
        'annotation_version': CATALOGUE_VERSION, 'source_rows': len(source_records),
        'catalogue_records': len(records), 'source_records': len({record['source_record_key'] for record in records}),
        'datasets': dict(datasets), 'facet_coverage': dict(coverage),
        'records_requiring_review': sum(record['review_required'] for record in records),
        'issue_counts': dict(Counter(issue for record in records for issue in record['issues'])),
        'exact_description_keys': len(lookup.exact_index),
        'ambiguous_description_keys': sum(len(keys) > 1 for keys in lookup.exact_index.values()),
        'source_backed_records': sum(bool(record['source_facets']) for record in records),
        'source_code_records': sum(bool(record['metadata'].get('FoodEx2 code')) for record in records),
        'foodon_whole_record_lookup_candidates': sum(bool(record['foodon_lookup']['whole_record_candidates']) for record in records),
        'scope': 'Retained source metadata only; includes incomplete/composite/non-production foods. No nutrient eligibility or matching accuracy inferred.',
        'coverage_interpretation': 'Counts are annotation availability, not correctness. Method-evidence counts overlap; unknown does not mean a property is absent.',
    }
    manifest = {
        'annotation_version': CATALOGUE_VERSION,
        'inputs': {key: {'path': str(path.relative_to(repo_root)), 'sha256': sha256(path)} for key, path in paths.items()},
        'code_sha256': {str(path.relative_to(repo_root)): sha256(path) for path in [Path(__file__).resolve(), repo_root / 'src/recipe_wrangler/matching/composition_catalogue.py', repo_root / 'src/recipe_wrangler/matching/facets.py', repo_root / 'src/recipe_wrangler/matching/identity.py']},
        'foodex2_release': catalogue.version, 'foodex2_sha256': catalogue.sha256,
        'policies': ['No model, database, vector index, or network calls.',
                     'Source IDs and releases are preserved; conflicting metadata variants are retained and flagged.',
                     'Literal description index preserves all qualifiers and word order; no automatic aliases.',
                     'Source code/field mappings are local semantic interpretations, not expert validation.',
                     'Native FoodEx2 codes are not full nutritional-equivalence assertions.',
                     'USDA not included in selectable catalogue; European sourcing unchanged.',
                     'Nutrients, preparation yields and live-table eligibility are outside this descriptor catalogue.'],
        'known_omissions': ['Reviewed supplemental EU records not re-imported here.', 'No native European LanguaL record assignments were available in these retained inputs.', 'Legacy rule proposals have incomplete contextual interpretation; review flagged cases.'],
    }
    output.mkdir(parents=True, exist_ok=False)
    with (output / 'records.jsonl').open('x', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    for name, data in [('coverage.json', report), ('manifest.json', manifest),
                       ('exact_index.json', {key: sorted(value) for key, value in sorted(lookup.exact_index.items())}),
                       ('reviewed_aliases.json', {'aliases': [], 'status': 'No full-phrase aliases have been reviewed for this catalogue.'}),
                       ('mappings.json', {'foodex2_base_terms': FOODEX2_BASE_MAPPINGS, 'physical_state_fields': PHYSICAL_STATE_MAPPINGS, 'review_status': 'local_semantic_review_not_expert_gold'})]:
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    with (output / 'review_worklist.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['catalogue_key', 'dataset', 'record_id', 'label', 'issues', 'human_review_status', 'human_correction'])
        writer.writeheader()
        for record in records:
            if record['review_required']:
                writer.writerow({'catalogue_key': record['catalogue_key'], 'dataset': record['source']['dataset'],
                                 'record_id': record['source']['record_id'], 'label': record['label'],
                                 'issues': ';'.join(record['issues']), 'human_review_status': 'pending'})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    report = build(args.repo_root.resolve(), args.output_dir)
    print(json.dumps({key: value for key, value in report.items() if key != 'facet_coverage'}, indent=2))


if __name__ == '__main__':
    main()
