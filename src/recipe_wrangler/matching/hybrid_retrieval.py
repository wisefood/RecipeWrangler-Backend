"""Offline union of lexical, facet and vector candidates; no acceptance threshold."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from recipe_wrangler.matching.evaluation.annotation_experiment import prepare_phrase
from recipe_wrangler.matching.composition_catalogue import exact_description_key
from recipe_wrangler.matching.ingredient_dictionary import normalize_label
from recipe_wrangler.matching.normalization import normalized_value


VERSION = 'hybrid_retrieval_v1_development'
FACET_FIELDS = ('biological_source', 'food_part', 'preparation_state', 'skin_status',
    'bone_status', 'physical_form', 'packing_medium', 'drainage_status', 'fat_level',
    'fat_content_description', 'grain_refinement', 'sodium_level', 'sugar_level',
    'derived_substance', 'reconstitution_state', 'processes', 'preservation_methods')


def lexical_tokens(text: str) -> tuple[str, ...]:
    """Normalize spelling/word order for retrieval only; retain material modifiers."""
    text = normalize_label(prepare_phrase(text))
    for pattern, value in [
        (r'\b(?:without|w o) (?:bones? and skin|skin and bones?)\b', 'bone_absent skin_absent'),
        (r'\b(?:skinless|without skin|w o skin)\b', 'skin_absent'),
        (r'\b(?:with skin|w skin)\b', 'skin_present'),
        (r'\b(?:boneless|without bones?|w o bones?)\b', 'bone_absent'),
        (r'\b(?:with bones?|bone in|on the bone)\b', 'bone_present'),
        (r'\buncooked\b', 'raw'),
    ]:
        text = re.sub(pattern, value, text)
    return tuple(word for word in text.split() if word not in {'the', 'a', 'an'})


def facet_items(annotation: dict):
    for field in FACET_FIELDS:
        value = annotation['facets'].get(field)
        for item in value if isinstance(value, (list, tuple, set, frozenset)) else [value]:
            if item is not None and item != 'unknown':
                yield field, normalized_value(field, item)


class HybridRetrievalIndex:
    """Keep each retrieval route's budget; never filter vector hits by missing facets."""

    def __init__(self, groups: dict[str, list[dict]], annotations: dict[str, dict]):
        self.groups, self.keys = groups, sorted(groups)
        self.labels = {k: groups[k][0]['label'] for k in self.keys}
        self.positions = {k: i for i, k in enumerate(self.keys)}
        self.lexical = defaultdict(dict)
        self.normalized = defaultdict(list)
        self.lengths = {}
        self.facets = defaultdict(set)
        self.record_group = {}
        for key in self.keys:
            counts = Counter(lexical_tokens(self.labels[key]))
            self.lengths[key] = sum(counts.values())
            self.normalized[tuple(sorted(counts.elements()))].append(key)
            for word, count in counts.items():
                self.lexical[word][key] = count
            for record in groups[key]:
                record_key = record['catalogue_key']
                self.record_group[record_key] = key
                annotation = annotations[record_key]
                identity = annotation['text_annotation']['main_ingredient']
                if identity:
                    self.facets[('main_ingredient', normalized_value('identity', identity))].add(record_key)
                for item in facet_items(annotation):
                    self.facets[item].add(record_key)
        self.average_length = sum(self.lengths.values()) / max(1, len(self.keys))

    def retrieve(self, text: str, query: dict, vector_scores, limit: int = 20) -> list[dict]:
        if limit < 1:
            return []
        if len(vector_scores) != len(self.keys):
            raise ValueError('Vector scores must follow index.keys')
        words = lexical_tokens(text)
        lexical = defaultdict(float)
        for word in set(words):
            posting = self.lexical.get(word, {})
            idf = math.log(1 + (len(self.keys) - len(posting) + .5) / (len(posting) + .5))
            for key, frequency in posting.items():
                length = self.lengths[key] / max(1, self.average_length)
                lexical[key] += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length))
        lexical_rank = sorted(lexical, key=lambda k: (-lexical[k], k))
        facet_keys = set()
        items = list(facet_items(query))
        identity = query['main_ingredient']
        if identity and items and len(query['foods']) == 1:
            # Intersect record IDs, not grouped labels: properties from separate
            # source variants must never manufacture a fully annotated record.
            records = set(self.facets.get(('main_ingredient', normalized_value('identity', identity)), ()))
            for item in items:
                records.intersection_update(self.facets.get(item, ()))
            facet_keys = {self.record_group[r] for r in records}
        facet_rank = sorted(facet_keys, key=lambda k: (-lexical.get(k, 0), k))
        vector_rank = sorted(self.keys, key=lambda k: (-float(vector_scores[self.positions[k]]), k))[:limit]
        exact = exact_description_key(text)
        routes = {'exact_label': [exact] if exact in self.groups else [],
            'normalized_exact': self.normalized.get(tuple(sorted(words)), [])[:limit],
            'lexical': lexical_rank[:limit], 'facets': facet_rank[:limit], 'vector': vector_rank}
        merged = {}
        for method, keys in routes.items():
            for rank, key in enumerate(keys, 1):
                item = merged.setdefault(key, {'key': key, 'label': self.labels[key],
                    'cosine': float(vector_scores[self.positions[key]]), 'rrf_score': 0.,
                    'retrieval_paths': [], 'records': []})
                item['rrf_score'] += 1 / (60 + rank)
                item['retrieval_paths'].append({'method': method, 'rank': rank,
                    'lexical_score': lexical.get(key, 0),
                    'facet_constraints': [list(p) for p in items] if method == 'facets' else []})
        for key, item in merged.items():
            item['retrieval_reason'] = 'exact_label_and_vector' if key == exact else 'vector' if key in vector_rank else 'lexical_or_facets'
            item['records'] = list({r['catalogue_key']: {'catalogue_key': r['catalogue_key'],
                'source': r['source'], 'original_label': r['label'], 'issues': r.get('issues', []),
                'nutrition_eligibility': r.get('nutrition_eligibility', 'not_assessed')}
                for r in self.groups[key]}.values())
        # Keep the union, rather than truncating away one route's only good hit.
        return sorted(merged.values(), key=lambda h: (h['key'] != exact,
            -h['rrf_score'], -h['cosine'], h['key']))
