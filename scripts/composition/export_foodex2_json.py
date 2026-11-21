#!/usr/bin/env python3
"""Export retained FoodEx2 data locally; source terms never become runtime rules."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue


def export_catalogue(catalogue_path: Path, output_dir: Path) -> dict:
    catalogue = FoodEx2Catalogue(catalogue_path)
    hierarchies = {}
    with zipfile.ZipFile(catalogue_path) as archive, archive.open('MTX.xml') as stream:
        for _, element in ET.iterparse(stream, events=('end',)):
            if element.tag == 'hierarchy':
                code = element.findtext('hierarchyDesc/code')
                if not code or code in hierarchies:
                    raise ValueError(f'Missing or duplicate hierarchy: {code}')
                hierarchies[code] = {
                    'code': code,
                    'label': element.findtext('hierarchyDesc/label') or '',
                    'definition': element.findtext('hierarchyDesc/scopeNote') or '',
                    'applicability': element.findtext('hierarchyDesc/hierarchyApplicability') or '',
                    'version': element.findtext('hierarchyVersion/version') or '',
                }
                element.clear()
            elif element.tag == 'term':
                element.clear()
    # MTX assignments reference the catalogue-wide tree, which has no separate
    # hierarchy declaration in this archive. Identify this synthesized metadata.
    hierarchies.setdefault('MTX', {
        'code': 'MTX', 'label': 'Catalogue-wide hierarchy', 'definition': '',
        'applicability': 'catalogue', 'version': catalogue.version,
        'metadata_origin': 'exporter_description_of_implicit_catalogue_hierarchy',
    })
    terms = []
    for term in sorted(catalogue.terms.values(), key=lambda term: term.code):
        for hierarchy, parent in term.parents:
            if hierarchy not in hierarchies:
                raise ValueError(f'Unknown hierarchy: {hierarchy}')
            if parent != 'root' and parent not in catalogue.terms:
                raise ValueError(f'Unknown parent: {parent}')
        terms.append({
            'code': term.code, 'label': term.label, 'status': term.status,
            'definition': term.scope_note,
            'hierarchy_assignments': [{'hierarchy': h, 'parent_code': p} for h, p in term.parents],
            'implicit_facet_expressions': list(term.implicit_facets),
        })
    food_terms = [term for term in terms if any(a['hierarchy'] == 'report' for a in term['hierarchy_assignments'])]
    facets = []
    for code, hierarchy in sorted(hierarchies.items()):
        if hierarchy['applicability'] == 'attribute':
            facets.append({**hierarchy, 'terms': [
                {**term, 'parent_codes': [a['parent_code'] for a in term['hierarchy_assignments'] if a['hierarchy'] == code]}
                for term in terms if any(a['hierarchy'] == code for a in term['hierarchy_assignments'])
            ]})
    mappings = []
    for code, label, method in [('A07GL', 'Boiling', 'boiled'), ('A07GR', 'Frying', 'fried')]:
        term = catalogue.terms.get(code)
        if term is None or term.label != label or term.status != 'APPROVED':
            raise ValueError(f'Reviewed mapping target changed: {code}')
        if not any(h == 'process' for h, _ in term.parents):
            raise ValueError(f'Mapping target is not in process facet: {code}')
        mappings.append({
            'foodex2_code': code, 'foodex2_label': label,
            'internal_assertions': {'cooking_method': method, 'preparation_state': 'cooked'},
            'status': 'reviewed', 'review_type': 'local_semantic_review_not_external_expert_validation',
            'rationale': f'{label} describes an actual heat cooking process; the assertion applies only when text establishes it occurred.',
            'source_definition': term.scope_note,
            'runtime_enabled': False,
        })
    release = {
        'source': 'EFSA FoodEx2', 'catalogue_version': catalogue.version,
        'archive_filename': catalogue_path.name, 'archive_sha256': catalogue.sha256,
        'source_url': 'https://github.com/openefsa/efsa-catalogues/blob/master/ecf_catalogues/MTX.ecf',
        'term_count': len(terms), 'statuses': dict(Counter(t['status'] for t in terms)),
        'food_terms_scope': 'Reporting hierarchy membership; includes categories, not an exact-alias dictionary',
        'food_term_count': len(food_terms),
        'facet_counts': {facet['code']: len(facet['terms']) for facet in facets},
        'hierarchies': list(hierarchies.values()),
        'policies': [
            'All statuses retained for inspection; only approved terms are eligible for future reviewed runtime mappings.',
            'Parent relations are hierarchy-specific; category membership is not nutritional equivalence.',
            'Implicit expressions are retained verbatim, without deriving food facts.',
            'Source terminology and local reviewed mappings are separate; no runtime integration.',
        ],
    }
    payloads = {
        'release.json': release,
        'catalogue_terms.json': {'catalogue_version': catalogue.version, 'terms': terms},
        'food_terms.json': {'catalogue_version': catalogue.version, 'hierarchy': 'report', 'scope': release['food_terms_scope'], 'terms': food_terms},
        'facets.json': {'catalogue_version': catalogue.version, 'facets': facets},
        'reviewed_mappings.json': {'catalogue_version': catalogue.version, 'catalogue_sha256': catalogue.sha256, 'mappings': mappings},
    }
    # A fresh directory preserves frozen exports and prevents partial overwrites.
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        with (output_dir / name).open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    release = export_catalogue(args.catalogue, args.output_dir)
    print(json.dumps({key: release[key] for key in ('catalogue_version', 'term_count', 'food_term_count', 'facet_counts')}, indent=2))


if __name__ == '__main__':
    main()
