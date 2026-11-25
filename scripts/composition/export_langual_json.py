#!/usr/bin/env python3
"""Export inspected LanguaL tables to JSON without changing matcher behavior."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


def export_langual(source_dir: Path, output_dir: Path) -> dict:
    filenames = ('terms.csv', 'synonyms.csv', 'parents.csv', 'related_terms.csv',
                 'localized_terms.csv', 'languages.csv', 'summary.json')
    def rows(name: str) -> list[dict]:
        with (source_dir / name).open(encoding='utf-8', newline='') as stream:
            return list(csv.DictReader(stream))

    summary = json.loads((source_dir / 'summary.json').read_text())
    terms = rows('terms.csv')
    codes = {term['code'] for term in terms}
    if len(codes) != len(terms) or '' in codes:
        raise ValueError('Missing or duplicate LanguaL term code')
    for term in terms:
        for flag in ('active', 'classification_only'):
            if term[flag] not in {'0', '1'}:
                raise ValueError(f'Invalid {flag}: {term[flag]}')
            term[flag] = term[flag] == '1'
    synonyms, parents = rows('synonyms.csv'), rows('parents.csv')
    related, localized, languages = rows('related_terms.csv'), rows('localized_terms.csv'), rows('languages.csv')
    language_ids = {row['ID'] for row in languages}
    for row in synonyms + localized:
        if row['code'] not in codes or row['language_id'] not in language_ids:
            raise ValueError('Unknown term or language in label table')
    if any(row['relation'] != 'untyped_source_synonym' for row in synonyms):
        raise ValueError('Unexpected synonym relation; review source semantics')
    for row in parents:
        if any(row[key] not in codes for key in ('parent_code', 'child_code')):
            raise ValueError('Unknown parent or child code')
        if row['grandparent_context'] and row['grandparent_context'] not in codes:
            raise ValueError('Unknown grandparent context')
    for row in related:
        if any(row[key] not in codes for key in ('source_code', 'related_code')):
            raise ValueError('Unknown related-term code')
    counts = {'term_records': len(terms), 'synonym_rows': len(synonyms),
              'hierarchy_rows': len(parents), 'related_term_rows': len(related),
              'localized_term_rows': len(localized)}
    for name, count in counts.items():
        if summary[name] != count:
            raise ValueError(f'Source summary count mismatch: {name}')
    groups = []
    for code, metadata in sorted(summary['facets'].items()):
        entries = sorted((term for term in terms if term['facet'] == code), key=lambda term: term['code'])
        if len(entries) != metadata['terms']:
            raise ValueError(f'Facet count mismatch: {code}')
        groups.append({'code': code, 'name': metadata['name'], 'terms': entries})
    release = {
        'source': 'LanguaL', 'version': summary['version'],
        'source_url': 'https://www.langual.org/langual_Downloads.asp',
        'source_mdb_sha256': summary['source_mdb_sha256'],
        'input_sha256': {name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest() for name in filenames},
        'counts': counts, 'facet_counts': dict(Counter(term['facet'] for term in terms)),
        'source_inspection_summary': summary,
        'policies': [
            'Inactive and classification-only terms retained; neither is automatically eligible for indexing.',
            'Synonyms remain untyped source synonyms, not exact aliases.',
            'Original language codes, definitions, additional information and relationship context retained.',
            'Normalized related-term IDs and raw source IDs both retained.',
            'No automatic FoodEx2 or FoodOn crosswalks; no matcher integration.',
        ],
    }
    payloads = {
        'release.json': release,
        'terms.json': {'version': summary['version'], 'terms': sorted(terms, key=lambda term: term['code'])},
        'facets.json': {'version': summary['version'], 'facets': groups},
        'synonyms.json': {'version': summary['version'], 'relation_policy': 'untyped_source_synonym', 'synonyms': synonyms},
        'localized_labels.json': {'version': summary['version'], 'languages': languages, 'labels': localized},
        'relationships.json': {'version': summary['version'], 'parents': parents, 'related_terms': related},
        'reviewed_mappings.json': {'version': summary['version'], 'status': 'not_yet_reviewed', 'runtime_enabled': False, 'mappings': []},
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        with (output_dir / name).open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    release = export_langual(args.source_dir, args.output_dir)
    print(json.dumps(release['counts'], indent=2))


if __name__ == '__main__':
    main()
