"""Bounded LanguaL derived-substance attachment for offline proposals."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


class LangualVocabulary:
    """Resolve the actual descriptor; local wording/attachment rules remain provisional."""

    def __init__(self, payload: dict, sha256: str = ''):
        self.version, self.sha256 = payload['version'], sha256
        self.stock = next(t for t in payload['terms'] if t['code'] == 'C0170')
        if (self.stock['facet'], self.stock['label'], self.stock['active']) != (
                'C', 'BROTH OR STOCK', True):
            raise ValueError('Expected active LanguaL C0170 BROTH OR STOCK')

    @classmethod
    def load(cls, path: Path):
        raw = path.read_bytes()
        return cls(json.loads(raw), hashlib.sha256(raw).hexdigest())

    def attach(self, annotation: dict, vocabulary) -> None:
        text = annotation['original_text']
        annotation['langual_provenance'] = {'version': self.version, 'sha256': self.sha256}
        # shortcut: only a single affirmative leading food phrase; expand with
        # reviewed scope examples before annotating components and alternatives.
        if len(annotation['foods']) > 1 or re.search(r'\b(?:with|and|or|without|not|no|for)\b', text, re.I):
            return
        match = re.match(r'(?:(?P<source>[\w -]+?)[ ,]+)?(?P<product>broth|stock)\b', text, re.I)
        if not match:
            return
        tail = text[match.end():]
        if re.search(r'\b(?:cubes?|powder|granulat\w*|paste|concentrat\w*|dehydrat\w*|dried)\b', text, re.I):
            return
        source = None
        source_span = None
        if match['source']:
            source_span = match.span('source')
        elif tail.startswith(','):
            source_match = re.match(r',\s*([^,]+)', tail)
            if source_match:
                start = match.end() + source_match.start(1)
                source_span = (start, match.end() + source_match.end(1))
        if source_span:
            start, end = source_span
            hits = [h for h in vocabulary.recognize(text[start:end])
                    if h['facet'] == 'source' and h['start'] == 0 and h['end'] == end-start
                    and vocabulary.terms[('source', h['code'])]['animal']]
            if len(hits) != 1:
                if match['source']:
                    return
                source_span = None
            else:
                source = hits[0]
        if annotation['foods']:
            head = annotation['foods'][0]
            product_overlap = head['start'] < match.end() and head['end'] > match.start('product')
            source_head = source_span and source_span[0] <= head['start'] and head['end'] <= source_span[1]
            if not product_overlap and not source_head:
                return
            if source_head:
                # A source-only head must not carry a meat concept into stock.
                head['concept_candidates'] = []
                head['family'], head['family_ambiguous'] = 'generic', False
                head['start'], head['end'] = 0, max(match.end(), source_span[1])
                head['text'] = text[:head['end']]
        else:
            head = {'food_id': 'food:0', 'start': 0, 'end': match.end(),
                    'text': match.group(), 'family': 'generic', 'family_ambiguous': False,
                    'polarity': 'asserted', 'scope_start': 0, 'scope_end': len(text),
                    'lexicalized_name': True, 'concept_candidates': []}
            annotation['foods'] = [head]
        identity = (source['value'] + ' ' if source else '') + 'stock'
        head['identity'] = annotation['main_ingredient'] = identity
        annotation['facets']['base_identity'] = identity
        annotation['facets']['derived_substance'] = 'broth_or_stock'
        start, end = match.span('product')
        event = {'field': 'derived_substance', 'value': 'broth_or_stock',
            'quote': text[start:end], 'start': start, 'end': end,
            'target_food_id': head['food_id'], 'polarity': 'asserted',
            'langual_code': 'C0170', 'catalogue_version': self.version,
            'rule': 'langual_leading_broth_stock', 'review_status': 'unreviewed_proposal'}
        annotation['modifiers'].append(event)
        annotation['evidence'].append({**event, 'field': 'facets.derived_substance'})
        annotation['evidence'].append({'field': 'main_ingredient', 'quote': text[:match.end()],
            'polarity': 'asserted', 'rule': 'langual_source_substance_identity'})
        covered = [(0, match.end())]
        if source:
            annotation['facets']['biological_source'] = source['value']
            start, end = source_span
            event = {**source, 'start': start, 'end': end, 'quote': text[start:end],
                'target_food_id': head['food_id'], 'polarity': 'asserted',
                'rule': 'foodex2_source_of_langual_stock', 'foodex2_code': source['code']}
            annotation['modifiers'].append(event)
            annotation['evidence'].append({**event, 'field': 'facets.biological_source'})
            covered.append(source_span)
        annotation['unresolved'] = [u for u in annotation['unresolved']
            if u.get('reason') != 'unknown_identity_preserved'
            and not any(a <= u['start'] and u['end'] <= b for a, b in covered)]
        annotation['review_reasons'] = [r for r in annotation['review_reasons']
            if r != 'unknown_food_head' and not (r == 'unexplained_text' and not annotation['unresolved'])]
