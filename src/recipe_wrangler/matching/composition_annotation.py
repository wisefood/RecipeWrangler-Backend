"""Offline contextual annotation of composition records; never nutrient approval."""
from __future__ import annotations

import copy
import re

from recipe_wrangler.matching.composition_catalogue import (
    FOODEX2_BASE_MAPPINGS, PHYSICAL_STATE_MAPPINGS,
)
from recipe_wrangler.matching.facet_vocabulary import PROCESS_CODES
from recipe_wrangler.matching.normalization import normalized_value


VERSION = 'contextual_composition_facets_v2_normalized'


def annotate_composition(record: dict, annotator, catalogue, *, text_annotation: dict | None = None) -> dict:
    """Keep text and native assertions separate; contradictory fields remain unknown."""
    annotation = copy.deepcopy(text_annotation) if text_annotation is not None else annotator.annotate(record['label'])
    facets = copy.deepcopy(annotation['facets'])
    native, evidence, issues = {}, [], []
    metadata = record.get('metadata', {})
    expression = str(metadata.get('FoodEx2 code') or '').strip()
    resolved = None
    if expression and expression not in {'N_A', 'Not Available'}:
        resolved = catalogue.resolve(expression)
        issues.extend(resolved.issues)
        if not resolved.issues:
            base = expression[:5]
            if base in FOODEX2_BASE_MAPPINGS:
                native.update(copy.deepcopy(FOODEX2_BASE_MAPPINGS[base][1]))
            by_code = {code: value for value, (code, _) in PROCESS_CODES.items()}
            # Only explicit processing descriptors. Preserve implicit expressions
            # as evidence until their inheritance/applicability has been reviewed.
            for facet, code in re.findall(r'(F\d{2})\.([A-Z0-9]{5})', expression):
                if facet == 'F28' and code in by_code:
                    term = annotator.vocabulary.terms.get(('process', code))
                    if term and term['label'] == PROCESS_CODES[by_code[code]][1]:
                        native.setdefault('processes', []).append(by_code[code])
                        native['preparation_state'] = 'cooked'
                    else:
                        issues.append('invalid_process_descriptor:' + code)
                else:
                    issues.append('unprojected_native_descriptor:' + facet + '.' + code)
        evidence.append({'source_field': 'FoodEx2 code', 'expression': expression,
            'catalogue_version': catalogue.version, 'catalogue_sha256': catalogue.sha256,
            'terms': [{'code': t.code, 'label': t.label, 'status': t.status,
                       'implicit_expressions': list(t.implicit_facets)} for t in resolved.terms],
            'assertions': copy.deepcopy(native), 'review_status': 'local_mapping_unreviewed'})
    state = metadata.get('Physical state FoodEx2')
    if state in PHYSICAL_STATE_MAPPINGS:
        native['physical_form'] = PHYSICAL_STATE_MAPPINGS[state]
        evidence.append({'source_field': 'Physical state FoodEx2', 'source_text': state,
                         'assertions': {'physical_form': native['physical_form']},
                         'review_status': 'local_mapping_unreviewed'})
    for field, value in native.items():
        if isinstance(value, list):
            value = sorted(set(value))
            native[field] = value
        current = normalized_value(field, facets.get(field))
        value = normalized_value(field, value)
        if field == 'physical_form' and value == 'powder' and facets.get('reconstitution_state') == 'reconstituted':
            issues.append('native_physical_form_basis_unspecified')
            facets[field] = None
            continue
        if current in (None, [], ''):
            facets[field] = copy.deepcopy(value)
        elif current != value:
            issues.append('source_label_disagreement:' + field)
            facets[field] = [] if isinstance(value, list) else None
    # Raw/cooked conflict must invalidate the method assertion as well.
    if 'source_label_disagreement:preparation_state' in issues:
        facets['processes'] = []
    if any(m['field'] == 'processes' and m['polarity'] == 'negated'
           and m['value'] in native.get('processes', []) for m in annotation['modifiers']):
        issues.append('source_label_negated_process')
        facets['processes'], facets['preparation_state'] = [], None
    return {'annotation_version': VERSION, 'source': record['source'],
        'catalogue_key': record.get('catalogue_key'),
        'source_record_key': record.get('source_record_key'), 'label': record['label'],
        'metadata': metadata, 'source_rows': record.get('source_rows'),
        'facets': facets, 'text_annotation': annotation, 'native_facets': native,
        'native_evidence': evidence, 'issues': issues,
        'review_required': True, 'approval_status': 'unreviewed_proposal',
        'production_eligible': False, 'nutrition_eligibility': 'not_assessed'}
