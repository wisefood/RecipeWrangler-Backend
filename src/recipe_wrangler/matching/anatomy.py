"""Read explicit anatomy from a single animal-food head, including frozen labels."""
from __future__ import annotations

import copy
import re
from functools import lru_cache

from .facets import _SOURCES, part_matches

VERSION = 'scoped_anatomy_v2'


def animal_head_source(head: dict) -> str | None:
    """A literal species head is usable even when ontology family links are missing."""
    if head.get('family_ambiguous') or head.get('polarity', 'asserted') != 'asserted':
        return None
    identity = head.get('identity', '')
    return next((value for value, pattern in _SOURCES.items()
                 if re.fullmatch(pattern, identity, re.I)), None)


def explicit_anatomy(annotation: dict) -> dict:
    """Fill quoted omissions only; never infer anatomy from nutrients or another food.

    This is a bounded label interpretation, not a corpus reparse. Existing facts,
    conflicts, compound heads and absent skin/bone assertions are not invented.
    """
    foods = annotation.get('foods', [])
    if len(foods) != 1 or not annotation.get('main_ingredient'):
        return annotation
    head = foods[0]
    source = animal_head_source(head)
    if source is None or annotation['main_ingredient'] != head.get('identity'):
        return annotation
    text = annotation.get('parsed_text') or annotation.get('input_text') or annotation.get('original_text', '')
    if not text or re.search(r'\bor\b|\balternatively\b', text, re.I):
        return annotation
    result = copy.deepcopy(annotation)
    claims = [('biological_source', source, match) for match in re.finditer(_SOURCES[source], text, re.I)
              if head.get('start', 0) <= match.start() < head.get('end', len(text))]
    brackets = [match.span() for match in re.finditer(r'\([^()]*\)', text)]
    parts = [(value, match) for value, match in part_matches(text)
             if not value.startswith('egg_') and value != 'skin']
    outside = [(value, match) for value, match in parts
               if not any(a <= match.start() < b for a, b in brackets)]
    for value, match in outside:
        prefix = text[max(0, match.start()-40):match.start()]
        if match.start() >= head.get('end', 0) and re.search(
                r'\b(?:with|in|for|from|containing)\b', text[head.get('end', 0):match.start()], re.I):
            continue
        if re.search(r'\b(?:without|not|no|for|from)\s*$', prefix, re.I):
            continue
        if value == 'leg' and re.search(r'\b(?:high|upper)\s*$', prefix, re.I):
            continue
        if value == 'skin' and any(v not in {'skin', 'fillet'} for v, _ in outside):
            continue
        # "Breast fillet" names the anatomical cut, not a second competing part.
        if value == 'fillet' and any(v not in {'fillet', 'skin'} for v, _ in outside):
            continue
        claims.append(('food_part', value, match))
    for field in ('biological_source', 'food_part'):
        supported = [(value, match) for key, value, match in claims if key == field]
        if len({value for value, _ in supported}) != 1:
            continue
        if any(field in reason and any(word in reason for word in ('conflict', 'disagreement'))
               for reason in annotation.get('review_reasons', [])):
            continue
        value, match = supported[0]
        previous = result['facets'].get(field)
        if previous not in (None, '', 'unknown'):
            continue
        result['facets'][field] = value
        result.setdefault('evidence', []).append({'field': 'facets.' + field,
            'quote': match.group(), 'polarity': 'asserted', 'rule': VERSION})
        result.setdefault('modifiers', []).append({'field': field, 'value': value,
            'quote': match.group(), 'start': match.start(), 'end': match.end(),
            'polarity': 'asserted', 'target_food_id': head['food_id'], 'rule': VERSION})
        result.setdefault('label_evidence_recovery', []).append({'field': field,
            'previous': previous, 'value': value, 'quote': match.group(), 'version': VERSION})
    return result


@lru_cache(maxsize=1)
def _label_annotator():
    # Dictionary/vocabulary parsing only; no embeddings or generative model.
    from recipe_wrangler.matching.runtime import build_annotator
    return build_annotator()


def recover_meat_fat_scope(annotation: dict) -> dict:
    """Reinterpret only the identified frozen conjunction fault, retaining its audit."""
    foods = annotation.get('foods') or []
    text = annotation.get('input_text') or annotation.get('original_text') or ''
    if (len(foods) != 2 or annotation.get('main_ingredient')
            or animal_head_source(foods[0]) is None or foods[1].get('identity') != 'fat'
            or any('conflict' in reason or 'disagreement' in reason
                   for reason in annotation.get('review_reasons', []))):
        return annotation
    parsed = annotation.get('parsed_text') or text
    if not any(match.start() <= foods[1].get('start', -1) < match.end()
               for match in re.finditer(r'\blean and fat\b', parsed, re.I)):
        return annotation
    revised = _label_annotator().annotate(text)
    if len(revised.get('foods', [])) != 1 or revised.get('main_ingredient') != foods[0].get('identity'):
        return annotation
    revised.setdefault('label_scope_recovery', []).append({
        'version': 'meat_fat_scope_recovery_v1', 'previous_parser_version': annotation.get('parser_version'),
        'previous_food_heads': copy.deepcopy(foods), 'quote': text})
    for field, value in revised['facets'].items():
        previous = (annotation.get('facets') or {}).get(field)
        if previous != value:
            revised.setdefault('label_evidence_recovery', []).append({
                'field': field, 'previous': previous, 'value': value, 'quote': text,
                'version': 'meat_fat_scope_recovery_v1'})
    return revised


def source_anatomy(facts: dict) -> dict:
    """Apply the same quoted-label interpretation to cached source facts."""
    text = facts.get('text_annotation')
    if not text:
        return facts
    interpreted = explicit_anatomy(recover_meat_fat_scope(text))
    if interpreted == text:
        return facts
    result = copy.deepcopy(facts)
    result['text_annotation'] = interpreted
    native = result.get('native_annotation')
    if native:
        for key in ('base_identity', 'biological_source', 'food_part', 'preparation_state', 'meat_fat_description'):
            if native['facets'].get(key) in (None, '', 'unknown') and not any(
                    key in issue for issue in native.get('issues', [])):
                native['facets'][key] = interpreted['facets'].get(key)
        native['text_annotation'] = interpreted
    if 'unknown_properties' in result:
        values = native['facets'] if native else interpreted['facets']
        result['unknown_properties'] = [key for key, value in values.items() if value in (None, 'unknown')]
    return result
