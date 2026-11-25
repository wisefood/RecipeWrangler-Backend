"""Food-independent grammar for explicit flavour and ingredient refinements."""
from __future__ import annotations

import re

from recipe_wrangler.matching.ingredient_dictionary import normalize_label


VERSION = 'product_refinements_v1_development'
WORDS = re.compile(r'[^\W_]+(?:[-\'][^\W_]+)*', re.UNICODE)
FLAVOUR = re.compile(r'\bflavou?r(?:ed|ing)?\b', re.I)


def flavour_prefix_spans(text: str, parser) -> list[tuple[int, int]]:
    """Exclude a named leading flavour from food-head selection, not the product."""
    spans = []
    for marker in re.finditer(r'\bflavou?red\b', text, re.I):
        if re.match(r'\s+(?:with|of)\b', text[marker.end():], re.I):
            continue
        start = max(text.rfind(',', 0, marker.start()), text.rfind(';', 0, marker.start())) + 1
        name = _name(text, start, marker.start(), parser, before=True)
        if name:
            spans.append((name[0], marker.end()))
    return spans


def _name(text: str, start: int, end: int, parser, before: bool):
    words = list(WORDS.finditer(text, start, end))
    candidates = []
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 6) + 1):
            a, b = words[i].start(), words[j-1].end()
            if before and text[b:end].strip() or not before and text[start:a].strip():
                continue
            hits = parser._lookup(text[a:b])
            if hits:
                candidates.append((-(b-a), a, b, hits))
    if candidates:
        _, a, b, hits = min(candidates, key=lambda c: (c[0], c[1]))
        return a, b, [{'term_id': h.term_id, 'label': h.preferred_label} for h in hits]
    # An explicit marker may name a flavour absent from FoodOn. Preserve that
    # word as a lexical proposal, without inventing an ontology link.
    if words:
        word = words[-1] if before else words[0]
        if word.group().casefold() not in {'no', 'not', 'without', 'natural', 'artificial', 'added', 'for', 'with', 'of', 'or', 'and'} and not word.group().casefold().endswith('ly'):
            return word.start(), word.end(), []
    return None


def attach_product_identity(annotation: dict, parser) -> None:
    if len(annotation['foods']) != 1 or not annotation['main_ingredient']:
        return
    head = annotation['foods'][0]
    text = annotation['original_text']
    annotation['product_identity'] = {'version': VERSION, 'base_identity': annotation['main_ingredient'],
        'lexical_head': head['text'], 'head_concepts': head.get('concept_candidates', []), 'refinements': []}

    def name_value(start, end):
        hits = parser._lookup(text[start:end])
        labels = {normalize_label(h.matched_label) for h in hits}
        return next(iter(labels)) if len(labels) == 1 else normalize_label(text[start:end])

    def add(field, value, start, end, concepts=(), polarity='asserted'):
        if polarity == 'negated':
            field = {'flavours': 'excluded_flavours', 'added_ingredients': 'excluded_ingredients'}.get(field, field)
        if polarity in {'asserted', 'negated'}:
            values = annotation['qualifiers'].setdefault(field, [])
            if value not in values:
                values.append(value)
                values.sort()
        event = {'field': 'qualifiers.'+field, 'value': value, 'start': start, 'end': end,
            'quote': text[start:end], 'target_food_id': head['food_id'], 'polarity': polarity,
            'rule': 'explicit_product_refinement', 'concept_candidates': list(concepts),
            'review_status': 'unreviewed_proposal'}
        annotation['modifiers'].append(event)
        annotation['evidence'].append(event.copy())
        annotation['product_identity']['refinements'].append(event.copy())
        annotation['unresolved'] = [u for u in annotation['unresolved']
            if not (start <= u['start'] and u['end'] <= end)]

    for marker in FLAVOUR.finditer(text):
        clause_start = max(text.rfind(',', 0, marker.start()), text.rfind(';', 0, marker.start())) + 1
        suffix = re.match(r'\s+(?:with|of)\s+', text[marker.end():], re.I)
        if not suffix and re.search(r'\b(?:no|without) (?:added )?$', text[clause_start:marker.start()], re.I):
            value = 'no_added_flavour' if re.search(r'\badded\s*$', text[clause_start:marker.start()], re.I) else 'unflavoured'
            add('flavour_status', value, clause_start, marker.end())
            continue
        if suffix:
            start = marker.end() + suffix.end()
            end = min([p for p in [text.find(',', start), text.find(';', start)] if p >= 0] or [len(text)])
            name = _name(text, start, end, parser, before=False)
        else:
            name = _name(text, clause_start, marker.start(), parser, before=True)
        if not name:
            add('flavour_status', 'flavoured', marker.start(), marker.end())
            continue
        start, end, concepts = name
        prefix = text[clause_start:start]
        polarity = 'negated' if re.search(r'\b(?:no|not|without)\s*$', prefix, re.I) else 'asserted'
        marker_prefix = text[clause_start:marker.start()]
        if re.search(r'\b(?:no|not|without)(?: added)?\s*$', marker_prefix, re.I):
            polarity = 'negated'
        if re.search(r'\bfor\s*$', prefix, re.I):
            polarity = 'intended'
        elif re.search(r'\bor\s*$', prefix, re.I):
            polarity = 'alternative'
        elif re.search(r'\band\s*$', prefix, re.I):
            polarity = 'unresolved'
        value = name_value(start, end)
        add('flavours', value, min(start, marker.start()), max(end, marker.end()), concepts, polarity)
        if polarity == 'asserted':
            annotation['qualifiers']['flavour_status'] = ['flavoured']
    for match in re.finditer(r'\bunflavou?red\b', text, re.I):
        add('flavour_status', 'unflavoured', match.start(), match.end())
    for marker in re.finditer(r'\b(?:contains?|containing|with added)\s+', text, re.I):
        start = marker.end()
        end = min([p for p in [text.find(',', start), text.find(';', start)] if p >= 0] or [len(text)])
        polarity = 'asserted'
        prefix = text[max(0, marker.start()-30):marker.start()]
        if re.search(r'\b(?:not|never)\s*$', prefix, re.I):
            polarity = 'negated'
        exclusion = re.match(r'(?:no|without)\s+', text[start:end], re.I)
        if exclusion:
            start += exclusion.end()
            polarity = 'negated'
        name = _name(text, start, end, parser, before=False)
        if name:
            a, b, concepts = name
            if polarity == 'asserted' and re.search(r'\bor\b', text[b:end], re.I):
                polarity = 'alternative'
            add('added_ingredients', name_value(a, b), marker.start(), b, concepts, polarity)
    if not annotation['unresolved']:
        annotation['review_reasons'] = [r for r in annotation['review_reasons'] if r != 'unexplained_text']
