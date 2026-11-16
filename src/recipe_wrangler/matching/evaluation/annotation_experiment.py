"""Diagnostic extraction comparison; never writes production annotations."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import fields
from typing import Literal

from pydantic import BaseModel, ConfigDict

from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary, normalize_label
from recipe_wrangler.matching.retrieval import _AMOUNT
from recipe_wrangler.matching.models import FoodFacets, RecipeContext


class SpanEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    quote: str
    polarity: Literal['asserted', 'negated', 'intended', 'alternative']


class FoodAnnotation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    main_ingredient: str | None = None
    facets: FoodFacets
    context: RecipeContext
    evidence: list[SpanEvidence]
    unresolved: list[str]


def prepare_phrase(text: str) -> str:
    """Use the existing quantity-prefix remover equally for both extractors."""
    return _AMOUNT.sub('', text).strip()


def baseline_annotation(text: str, dictionary: IngredientDictionary) -> dict:
    result = dictionary.extract(text)
    facets = dict(result['facets'])
    for name in ('processes', 'preservation_methods'):
        facets[name] = sorted(facets[name])
    return {
        'main_ingredient': result['main_ingredient'],
        'facets': facets,
        'context': {},
        'evidence': [],
        'baseline_evidence': result['extraction_evidence'],
        'unresolved': [result['context_warning']] if result['context_warning'] else [],
        'evidence_limitation': 'Legacy extractor emits facet strings, not validated text spans.',
    }


def validate_annotation(raw: dict, text: str) -> dict:
    """Validate schema, field paths and evidence text; not semantic correctness."""
    for container, model in [('facets', FoodFacets), ('context', RecipeContext)]:
        if not isinstance(raw.get(container), dict):
            raise ValueError(f'{container} must be an object')
        unknown = set(raw[container]) - {field.name for field in fields(model)}
        if unknown:
            raise ValueError(f'Unknown {container} fields: {sorted(unknown)}')
    annotation = FoodAnnotation.model_validate(raw)
    result = annotation.model_dump(mode='json')
    allowed = {'main_ingredient'} | {
        f'{container}.{field.name}'
        for container, model in [('facets', FoodFacets), ('context', RecipeContext)]
        for field in fields(model)
    }
    for evidence in result['evidence']:
        if evidence['field'] not in allowed:
            raise ValueError(f'Unknown evidence field: {evidence["field"]}')
        quote = evidence['quote']
        if not quote or quote not in text:
            raise ValueError('Evidence quote is not an exact nonempty substring')
        evidence['spans'] = [
            {'start': match.start(), 'end': match.end()}
            for match in re.finditer(re.escape(quote), text)
        ]
    supported = {item['field'] for item in result['evidence'] if item['polarity'] == 'asserted'}
    claims = {'main_ingredient': result['main_ingredient']}
    for container in ('facets', 'context'):
        claims.update({f'{container}.{key}': value for key, value in result[container].items()})
    # identity/base_identity duplicate the main field; model should leave them null.
    exempt = {'facets.identity', 'facets.base_identity'}
    missing = [path for path, value in claims.items() if value is not None and value != [] and path not in supported and path not in exempt]
    if missing:
        raise ValueError(f'Assertions lack supporting spans: {missing}')
    return result


def ground_annotation(annotation: dict, dictionary: IngredientDictionary, mappings: dict) -> dict:
    """Attach lookup candidates and reviewed process references in shadow only."""
    identity = annotation.get('main_ingredient')
    hits = dictionary.lookup(identity) if identity else ()
    processes = annotation.get('facets', {}).get('processes') or []
    return {
        'identity_status': 'resolved' if len(hits) == 1 else 'ambiguous' if hits else 'unresolved',
        'foodon_candidates': [{'term_id': hit.term_id, 'label': hit.preferred_label, 'label_type': hit.label_type} for hit in hits],
        'foodex2_process_references': [
            {'code': mapping['foodex2_code'], 'label': mapping['foodex2_label']}
            for mapping in mappings['mappings']
            if mapping['internal_assertions'].get('cooking_method') in processes
        ],
        'policy': 'Exact local lookup and reviewed process mappings only; no inferred IDs or composition decision.',
    }


def _value(annotation: dict, path: str):
    value = annotation
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _normalized(value):
    if isinstance(value, str):
        return normalize_label(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(_normalized(item) for item in value)
    return value


def compare_annotations(cases: list[dict], predictions: list[dict]) -> tuple[dict, list[dict]]:
    by_id = {case['case_id']: case for case in cases}
    if len(by_id) != len(cases):
        raise ValueError('Duplicate diagnostic case IDs')
    stats = defaultdict(lambda: {'checked': 0, 'agreed': 0, 'missed': 0, 'wrong_value': 0, 'unsupported_assertion': 0, 'unavailable': 0})
    comparisons = []
    seen = set()
    for prediction in predictions:
        case_id = prediction['case_id']
        if case_id not in by_id or case_id in seen:
            raise ValueError('Unknown or duplicate prediction ID')
        seen.add(case_id)
        case = by_id[case_id]
        for path, expected in case['expected'].items():
            actual = _value(prediction.get('annotation') or {}, path)
            if prediction.get('error'):
                outcome = 'unavailable'
            elif _normalized(actual) == _normalized(expected):
                outcome = 'agreed'
            elif expected is None or expected == []:
                outcome = 'unsupported_assertion'
            elif actual is None or actual == []:
                outcome = 'missed'
            else:
                outcome = 'wrong_value'
            stats[path]['checked'] += 1
            stats[path][outcome] += 1
            comparisons.append({'case_id': case_id, 'kind': case['kind'], 'field': path,
                                'expected': expected, 'actual': actual, 'outcome': outcome})
    if seen != set(by_id):
        raise ValueError('Missing diagnostic predictions')
    return dict(stats), comparisons


def normalize_collection_nulls(raw: dict) -> tuple[dict, list[str]]:
    """Adapt explicit nulls to empty sets only; preserve every semantic proposal."""
    import copy

    result = copy.deepcopy(raw)
    changed = []
    facets = result.get('facets')
    if isinstance(facets, dict):
        for name in ('processes', 'preservation_methods'):
            if name in facets and facets[name] is None:
                facets[name] = []
                changed.append(f'facets.{name}')
    return result, changed
