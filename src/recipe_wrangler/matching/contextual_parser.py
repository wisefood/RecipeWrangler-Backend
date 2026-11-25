"""Dictionary-backed food heads and explicitly scoped modifier proposals."""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from recipe_wrangler.matching.facets import _COOKING, _PARTS, _PRESERVATION, _PROCESSES, _SOURCES, part_matches

# Local extension: preserve the legacy extractor used by other experiments.
_COOKING = {**_COOKING, 'simmered': r'\bsimmer(?:ed|ing)?\b',
            'stewed': r'\bstew(?:ed|ing)?\b'}
_PROCESSES = {**_PROCESSES, **_COOKING}
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary, normalize_label
from recipe_wrangler.matching.models import FoodFacets, RecipeContext
from recipe_wrangler.matching.product_identity import flavour_prefix_spans
from recipe_wrangler.matching.anatomy import explicit_anatomy, animal_head_source
from recipe_wrangler.utils.food_identity import _SYNONYMS

PARSER_VERSION = 'contextual_rule_parser_v9_shared_anatomy_development'
PREFIX = 'http://purl.obolibrary.org/obo/'
# Reviewed experimental applicability families; ontology membership is not equivalence.
FAMILY_ROOTS = {
    'egg': ['FOODON_00001274', 'FOODON_02020001', 'FOODON_02010009'],
    'pasta': ['FOODON_00001211'],
    'cereal': ['FOODON_00001093', 'FOODON_00001056', 'FOODON_00001183'],
    'mustard': ['FOODON_00002052'],
    'milk': ['FOODON_00001257'],
    'sauce': ['FOODON_00001761'],
    'meat': ['FOODON_00001040', 'FOODON_02010107'],
}
WORDS = re.compile(r'[^\W_]+(?:[-’\'][^\W_]+)*', re.UNICODE)
LEADING_MODIFIERS = re.compile(r'^(?:raw|cooked|boiled|fried|scrambled|hard-boiled|soft-boiled|canned|tinned|frozen|dried|leftover|large|small|medium|ripe|smooth|sweet|ground|chopped|skinless|boneless|low|reduced|wholegrain)\b', re.I)


class ContextualParser:
    def __init__(self, dictionary: IngredientDictionary, families: dict[str, set[str]], provenance: dict | None = None):
        self.dictionary = dictionary
        self.families = families
        self.provenance = provenance or {}

    @classmethod
    def from_snapshots(cls, dictionary_path: Path, parents_path: Path) -> ContextualParser:
        dictionary = IngredientDictionary.load(dictionary_path)
        children = defaultdict(set)
        with parents_path.open(encoding='utf-8', newline='') as stream:
            for row in csv.DictReader(stream):
                children[row['parent_iri']].add(row['child_iri'])
        families = defaultdict(set)
        for family, roots in FAMILY_ROOTS.items():
            pending = [PREFIX + root for root in roots]
            seen = set()
            while pending:
                iri = pending.pop()
                if iri in seen:
                    continue
                seen.add(iri)
                families[iri].add(family)
                pending.extend(children[iri])
        return cls(dictionary, dict(families), {
            'dictionary_sha256': hashlib.sha256(dictionary_path.read_bytes()).hexdigest(),
            'named_parents_sha256': hashlib.sha256(parents_path.read_bytes()).hexdigest(),
            'family_roots': FAMILY_ROOTS,
        })

    def _lookup(self, phrase):
        hits = self.dictionary.lookup(phrase)
        if hits:
            return hits
        # FoodOn's generic naming suffix does not add a preparation qualifier.
        hits = self.dictionary.lookup(phrase + ' food product')
        if hits:
            return hits
        # A trailing ontology type is not part of the culinary name. Preserve
        # the complete dish head instead of reducing it to its first ingredient.
        hits = self.dictionary.lookup(phrase + ' (dish)')
        if hits:
            return hits
        # Conservative plural fallback is used only when the resulting name exists.
        words = normalize_label(phrase).split()
        singular = ' '.join(word[:-1] if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us')) else word for word in words)
        canonical = ' '.join(_SYNONYMS.get(word, word) for word in singular.split())
        for variant in dict.fromkeys((singular, canonical)):
            hits = (self.dictionary.lookup(variant) or self.dictionary.lookup(variant + ' food product')
                    or self.dictionary.lookup(variant + ' (dish)'))
            if hits:
                return hits
        return []

    def _stock_head(self, text, offset):
        sources = r'vegetable|chicken|beef|fish|mushroom|meat'
        match = re.search(rf'\b(?:(?P<prefix>{sources})\s+)?(?P<stock>stock|broth|bouillon)\b'
                          rf'(?:\s+(?:cubes?|powder|granules?|paste))?(?:,\s*(?P<suffix>{sources})\b)?', text, re.I)
        if not match or not re.fullmatch(
                r'\s*(?:(?:low|reduced)[- ](?:salt|sodium|fat)\s+)*', text[:match.start()], re.I):
            return None
        source = (match['prefix'] or match['suffix'] or '').casefold()
        identity = (source + ' stock').strip()
        # Explicit product syntax, not an inferred animal-meat head or concentration.
        hits = self._lookup(identity) or self._lookup(identity.replace('stock', 'broth'))
        return {'food_id': 'food:0', 'text': match.group(), 'start': offset + match.start(),
                'end': offset + match.end(), 'identity': identity, 'family': 'generic',
                'concept_candidates': [{'term_id': h.term_id, 'label': h.preferred_label} for h in hits],
                'family_ambiguous': False, 'polarity': 'asserted', 'lexicalized_name': False,
                'head_method': 'explicit_stock_product_grammar'}

    def _head(self, text, offset=0):
        stock = self._stock_head(text, offset)
        if stock:
            return stock
        tokens = list(WORDS.finditer(text))
        flavour_spans = flavour_prefix_spans(text, self)
        fat_modifier_spans = [m.span() for m in re.finditer(r'\b(?:low|reduced|whole|full)[- ]fat\b', text, re.I)]
        candidates, qualified_candidates = [], []
        for start in range(len(tokens)):
            for end in range(start + 1, min(len(tokens), start + 6) + 1):
                left, right = tokens[start].start(), tokens[end - 1].end()
                if any(a <= left and right <= b for a, b in [*flavour_spans, *fat_modifier_spans]):
                    continue
                phrase = text[left:right]
                if any(char in phrase for char in ',;'):
                    break
                if re.search(r'\b(?:in|with|without|for|packed)\b', phrase, re.I):
                    continue
                qualified = (LEADING_MODIFIERS.search(phrase) or any(
                    re.match(pattern, phrase, re.I) for pattern in (*_PROCESSES.values(), *_PRESERVATION.values()))) and normalize_label(phrase) not in self.dictionary.payload.get('lexicalized_identities', {})
                hits = self._lookup(phrase)
                # An explicitly wholegrain mustard phrase selects the condiment
                # sense; bare mustard remains unresolved if the dictionary lacks it.
                if normalize_label(phrase) == 'mustard' and re.search(r'\bwholegrain\b', text, re.I):
                    hits = self.dictionary.lookup('mustard (prepared)') or hits
                if not hits:
                    continue
                (qualified_candidates if qualified else candidates).append((left, -(right-left), right, phrase, hits))
        candidates = candidates or qualified_candidates
        if not candidates:
            return None
        left, _, right, phrase, hits = min(candidates, key=lambda item: (item[0], item[1]))
        # Unlisted adjacent noun compounds are right-headed: milk custard is
        # custard, icing sugar is sugar. Complete dictionary names keep priority.
        initial_families = set().union(*(self.families.get(hit.term_id, set()) for hit in hits))
        if 'sauce' not in initial_families and (len(WORDS.findall(phrase)) == 1 or not initial_families or initial_families == {'generic'}):
            while True:
                adjacent = [c for c in candidates if c[0] > right
                            and (text[right:c[0]].isspace() or re.fullmatch(r'\s+based\s+',text[right:c[0]],re.I))]
                if not adjacent:
                    break
                adjacent = [c for c in adjacent if normalize_label(c[3]) not in {'powder', 'cube', 'cubes', 'flakes', 'granules', 'fruit'}]
                if not adjacent:
                    break
                next_left = min(c[0] for c in adjacent)
                if WORDS.search(text, right).start() != next_left and not re.fullmatch(r'\s+based\s+',text[right:next_left],re.I):
                    break
                left, _, right, phrase, hits = min(adjacent, key=lambda c: (c[0], c[1]))
                if len(WORDS.findall(phrase)) > 1:
                    break
        families = set().union(*(self.families.get(hit.term_id, set()) for hit in hits))
        family = next(iter(families)) if len(families) == 1 else 'generic'
        identity = normalize_label(phrase)
        if len(hits) == 1 and len(WORDS.findall(phrase)) == 1 and len(WORDS.findall(hits[0].preferred_label)) == 1:
            identity = normalize_label(hits[0].preferred_label)
        elif len(hits) == 1 and len(WORDS.findall(phrase)) == 1:
            generic = re.sub(r' food product$| \(dish\)$', '', normalize_label(hits[0].preferred_label))
            if len(WORDS.findall(generic)) == 1:
                identity = generic
        if family in {'egg', 'pasta', 'mustard'}:
            identity = family
        elif family == 'milk' and re.fullmatch(
                r'(?:(?:semi[- ]|partly |partially )?skimmed |whole |whole fat |full fat )?milk(?: powder)?',
                identity):
            identity = 'milk'
        elif family == 'meat':
            sources = [name for name, pattern in _SOURCES.items() if re.search(pattern, phrase, re.I)]
            if len(sources) == 1:
                identity = {'cattle': 'beef', 'pig': 'pork'}.get(sources[0], sources[0])
        return {'food_id': 'food:0', 'text': phrase, 'start': offset + left, 'end': offset + right,
                'identity': identity, 'family': family,
                'concept_candidates': [{'term_id': hit.term_id, 'label': hit.preferred_label} for hit in hits],
                'family_ambiguous': len(families) > 1, 'polarity': 'asserted',
                'lexicalized_name': normalize_label(phrase) in self.dictionary.payload.get('lexicalized_identities', {})}

    def parse(self, text: str) -> dict:
        if not text.strip():
            raise ValueError('Ingredient text is required')
        head = self._head(text)
        facets = asdict(FoodFacets())
        facets['processes'], facets['preservation_methods'] = [], []
        context = asdict(RecipeContext())
        modifiers, evidence, unresolved, covered = [], [], [], set()
        foods = [head] if head else []
        reasons = []
        def consume(start, end):
            covered.update(range(start, end))
        def polarity(start):
            prefix = text[max(0, start-40):start]
            if re.search(r'\b(?:not|never)(?:\s+(?:fully|previously))?\s*$', prefix, re.I):
                return 'negated'
            if re.search(r'\bfor(?:\s+deep)?\s*$', prefix, re.I):
                return 'intended'
            return 'asserted'
        def add(field, value, match, status=None, rule='scoped_phrase'):
            state = status or polarity(match.start())
            target = next((food for food in reversed(foods) if food.get('scope_start', 0) <= match.start() < food.get('scope_end', len(text))), head)
            modifiers.append({'target_food_id': target['food_id'] if target else None,
                              'field': field, 'value': value, 'polarity': state,
                              'quote': match.group(), 'start': match.start(), 'end': match.end(), 'rule': rule})
            consume(match.start(), match.end())
        if head:
            # Same guard as the LanguaL broth/stock rule: concentrates and scoped phrases are not liquid stock.
            if head.get('head_method') == 'explicit_stock_product_grammar' and not re.search(
                    r'\b(?:cubes?|powder|granul\w*|paste|concentrat\w*|dehydrat\w*|dried|with|and|or|without|not|no|for)\b', text, re.I):
                facets['derived_substance'] = 'broth_or_stock'
            head['scope_start'], head['scope_end'] = 0, len(text)
            if re.search(r'\b(?:without|not|no)\s*$', text[:head['start']], re.I):
                head['polarity'] = 'excluded'
                reasons.append('excluded_or_negated_food_head')
            if re.search(r'\b(?:with|in)\b', text[:head['start']], re.I):
                head['polarity'] = 'component'
                reasons.append('only_component_food_head_identified')
            consume(head['start'], head['end'])
            if re.search(r'\b(?:with|and|or)\b', head['text'], re.I):
                reasons.append('compound_food_head_requires_review')
            if head['family_ambiguous']:
                reasons.append('ambiguous_food_family')
            if len(head['concept_candidates']) > 1:
                reasons.append('ambiguous_ontology_candidates')
            # Find actual additional food heads, excluding property conjunctions.
            for connector in re.finditer(r'\b(?:with|and)\b', text, re.I):
                if re.search(r'\bflavou?red\s*$', text[:connector.start()], re.I):
                    continue
                if connector.start() < head['end']:
                    continue
                tail = text[connector.end():]
                if ((head['family'] == 'meat' or animal_head_source(head) is not None)
                        and re.search(r'\blean\s*$', text[:connector.start()], re.I)
                        and re.match(r'\s*fat\b', tail, re.I)):
                    continue
                if re.match(r'\s*(?:bone|skin|seeds?)\s*(?:[,;]|$)', tail, re.I):
                    continue
                second = self._head(tail, connector.end())
                if second is None:
                    # In this connector position a species word is another food
                    # candidate, never automatically the producer of the first food.
                    for source, pattern in _SOURCES.items():
                        match = re.search(pattern, tail, re.I)
                        if match and re.fullmatch(r'\s*(?:(?:fried|boiled|cooked|raw)\s+)?', tail[:match.start()], re.I):
                            second = {'food_id': '', 'text': match.group(),
                                      'start': connector.end() + match.start(), 'end': connector.end() + match.end(),
                                      'identity': source, 'family': 'meat', 'concept_candidates': [],
                                      'family_ambiguous': False, 'polarity': 'asserted',
                                      'head_method': 'animal_food_candidate_after_connector'}
                            break
                prefix = text[connector.end():second['start']] if second else ''
                if second and re.fullmatch(r'\s*(?:(?:raw|cooked|boiled|fried|scrambled|fresh|canned|chopped|large|small|medium)\s+){0,3}', prefix, re.I):
                    foods[-1]['scope_end'] = connector.start()
                    second['scope_start'], second['scope_end'] = connector.end(), len(text)
                    second['food_id'] = f'food:{len(foods)}'
                    foods.append(second)
                    consume(second['start'], second['end'])
                    reasons.append('multiple_food_heads')
        else:
            reasons.append('unknown_food_head')
        family = head['family'] if head else None
        if head:
            # Producer attachment is supported only for typed animal/egg heads.
            for value, pattern in _SOURCES.items():
                for match in re.finditer(pattern, text, re.I):
                    if family in {'egg', 'meat'} and not any(food['start'] <= match.start() < food['end'] for food in foods[1:]):
                        if polarity(match.start()) == 'intended':
                            add('purpose', value, match, status='intended', rule='intended_target_not_producer')
                        else:
                            add('biological_source', value, match, rule='animal_source_for_typed_head')
            if family in {'egg', 'meat'}:
                parts = {**_PARTS, 'egg_white': r'\b(?:egg\s+)?whites?\b', 'egg_yolk': r'\b(?:egg\s+)?yolks?\b'}
                matches = ([(value, match) for value, pattern in parts.items() if value.startswith('egg_')
                            for match in re.finditer(pattern, text, re.I)] if family == 'egg'
                           else [(value, match) for value, match in part_matches(text)
                                 if not value.startswith('egg_')])
                for value, match in matches:
                    add('food_part', value, match, rule='part_for_typed_head')
                if family == 'meat':
                    # A suppressed cut word ("breast fillet") names the same cut: explained, not unresolved.
                    kept = {match.span() for _, match in matches}
                    for pattern in _PARTS.values():
                        for match in re.finditer(pattern, text, re.I):
                            if match.span() not in kept:
                                consume(match.start(), match.end())
            for value, pattern in _PROCESSES.items():
                for match in re.finditer(pattern, text, re.I):
                    if head.get('lexicalized_name') and head['start'] <= match.start() < head['end']:
                        continue
                    if (match.group().casefold() in {'baking','frying','roasting','cooking'}
                            and match.end() <= head['start'] and not text[:match.start()].strip()
                            and not text[match.end():head['start']].strip()):
                        add('processes',value,match,status='intended',rule='nominal_cooking_use_not_actual_state')
                        continue
                    # A gerund before another noun after the main food can describe
                    # a carrier/component (e.g. boiling water), not the main food.
                    following = WORDS.search(text, match.end())
                    if (match.start() >= head['end'] and match.group().casefold().endswith('ing')
                            and following and not text[match.end():following.start()].strip()
                            and following.group().casefold() not in {'in', 'with', 'and', 'or'}
                            and polarity(match.start()) == 'asserted'):
                        add('processes', value, match, status='unresolved', rule='process_attachment_not_established')
                        modifiers[-1]['target_food_id'] = None
                        reasons.append('process_attachment_unresolved')
                    else:
                        add('processes', value, match)
            for value, pattern in _PRESERVATION.items():
                for match in re.finditer(pattern, text, re.I):
                    add('preservation_methods', value, match)
            for match in re.finditer(r'\b(?:raw|uncooked|cooked)\b', text, re.I):
                add('preparation_state', 'raw' if match.group().casefold() != 'cooked' else 'cooked', match)
            if family == 'meat' or animal_head_source(head) is not None:
                for match in re.finditer(r'\b(?:extra[- ]lean|lean and fat|lean)\b', text, re.I):
                    add('meat_fat_description', match.group().casefold(), match)
            if family == 'egg':
                for match in re.finditer(r'\b(hard|soft)[ -]boiled\b', text, re.I):
                    add('cooking_extent', match.group(1).casefold() + '_boiled', match)
            # Whole-fat milk is a phrase, not a whole physical object.
            milk_fat_spans = []
            if family == 'milk':
                for match in re.finditer(r'\bwhole[ -]fat\b|\bwhole(?=\s+milk\b)', text, re.I):
                    add('fat_level', 'whole', match)
                    milk_fat_spans.append((match.start(), match.end()))
            for match in re.finditer(r'\bwhole\b', text, re.I):
                if family == 'egg' and not any(a <= match.start() < b for a, b in milk_fat_spans):
                    add('physical_form', 'whole', match)
            for match in re.finditer(r'\bwholegrain\b|\bwholemeal\b|\brefined\b', text, re.I):
                if family == 'cereal':
                    add('grain_refinement', 'refined' if match.group().casefold() == 'refined' else 'wholegrain', match)
                elif family == 'mustard':
                    add('product_style', match.group().casefold(), match)
                else:
                    reasons.append('grain_qualifier_attachment_unresolved')
            if family == 'cereal' and re.search(r'\bbrown\s+rice\b|\brice\s*,?\s*brown\b',text,re.I):
                for match in re.finditer(r'\bbrown\b',text,re.I):
                    add('grain_refinement','wholegrain',match)
            for value, pattern in {'powder': r'\bpowder(?:ed)?\b', 'cube': r'\bcubes?\b', 'paste': r'\bpaste\b'}.items():
                for match in re.finditer(pattern, text, re.I):
                    add('physical_form', value, match)
            for field, patterns in {
                'ripeness': {'ripe': r'\bripe\b', 'unripe': r'\bunripe\b'},
                'texture': {'smooth': r'\bsmooth\b', 'crunchy': r'\bcrunchy\b', 'runny': r'\brunny\b'},
                'thickness': {'thin': r'\bthin(?:ly)?\b', 'thick': r'\bthick(?:ly)?\b'},
                'size': {'large': r'\blarge\b', 'small': r'\bsmall\b', 'medium': r'\bmedium\b(?![- ]rare\b)'},
                'skin_status': {'absent': r'\bskinless\b|\b(?:without|w/o)\s+(?:bones? and )?skin\b|\b(?:skin(?:\s+and\s+bones?)?|bones?\s+and\s+skin)\s+removed\b', 'present': r'\bwith skin\b|\bw/\s+skin\b'},
                'bone_status': {'absent': r'\bboneless\b|\b(?:without|w/o)\s+(?:skin and )?bones?\b|\b(?:bones?(?:\s+and\s+skin)?|skin\s+and\s+bones?)\s+removed\b', 'present': r'\bwith bones?\b|\bon the bone\b|\bbone[- ]in\b'},
                'packing_medium': {'brine': r'\bin brine\b', 'oil': r'\bin oil\b', 'water': r'\bin water\b'},
            }.items():
                for value, pattern in patterns.items():
                    for match in re.finditer(pattern, text, re.I):
                        if field == 'packing_medium' and not re.search(r'\b(?:canned|tinned|packed)\b', text, re.I):
                            reasons.append('packing_or_cooking_medium_unresolved')
                            continue
                        add(field, value, match)
                        if field == 'size':
                            between = text[match.end():head['start']] if match.end() <= head['start'] else ''
                            target = 'piece' if re.search(r'\bslices?\b', between, re.I) else 'food'
                            add('size_target', target, match, rule='size_scope_before_food')
            for field, value, pattern in [
                ('context.optional', True, r'\boptional\b'),
                ('context.quantity_qualifier', 'to taste', r'\bto taste\b'),
                ('context.ingredient_role', 'garnish', r'\b(?:for garnish|to garnish|as garnish)\b'),
            ]:
                for match in re.finditer(pattern, text, re.I):
                    add(field, value, match, status='asserted', rule='recipe_usage')
        # Attach alternatives to their events, not to an actual process list.
        for first in modifiers:
            for second in modifiers:
                if first['field'] != second['field'] or first['end'] >= second['start']:
                    continue
                if re.fullmatch(r'\s+or\s+', text[first['end']:second['start']], re.I):
                    first['polarity'] = second['polarity'] = 'alternative'
                    reasons.append('alternative_modifier_values')
        # Project only supported asserted properties of a single food.
        scalar = defaultdict(list)
        if len(foods) == 1 and head['polarity'] == 'asserted':
            facets['base_identity'] = head['identity']
            evidence.append({'field': 'main_ingredient', 'quote': head['text'], 'polarity': 'asserted'})
            for item in modifiers:
                if item['polarity'] != 'asserted' or item['target_food_id'] != head['food_id']:
                    continue
                field, value = item['field'], item['value']
                if field in {'processes', 'preservation_methods'}:
                    facets[field].append(value)
                elif field.startswith('context.'):
                    context[field.split('.', 1)[1]] = value
                elif field in facets:
                    scalar[field].append(value)
                evidence.append({'field': field if field.startswith('context.') else 'facets.' + field,
                                 'quote': item['quote'], 'polarity': 'asserted'})
            for field, values in scalar.items():
                unique = set(values)
                if len(unique) == 1:
                    facets[field] = values[0]
                else:
                    reasons.append('conflicting_assertions:' + field)
            for field in ('processes', 'preservation_methods'):
                facets[field] = sorted(set(facets[field]))
            if set(facets['processes']) & set(_COOKING):
                if facets['preparation_state'] == 'raw':
                    facets['preparation_state'] = None
                    reasons.append('conflicting_actual_preparation')
                else:
                    facets['preparation_state'] = 'cooked'
            elif any(item['field'] == 'processes' and item['polarity'] == 'alternative' for item in modifiers):
                alternatives = [item['value'] for item in modifiers if item['field'] == 'processes' and item['polarity'] == 'alternative']
                if alternatives and all(value in _COOKING for value in alternatives):
                    facets['preparation_state'] = 'cooked'
            if 'drained' in facets['processes']:
                facets['drainage_status'] = 'drained_and_rinsed' if 'rinsed' in facets['processes'] else 'drained'
        if len(foods) > 1:
            facets['compound'] = True
        for token in WORDS.finditer(text):
            if all(position in covered for position in range(token.start(), token.end())):
                continue
            word = token.group().casefold()
            # Grammar words are ignored only after food/modifier roles are recorded.
            if word in {'a', 'an', 'the', 'of', 'and', 'with', 'without', 'not', 'never', 'for', 'or'}:
                continue
            unresolved.append({'text': token.group(), 'start': token.start(), 'end': token.end(),
                               'reason': 'unattached_or_unsupported_word'})
        if unresolved:
            reasons.append('unexplained_text')
        if not head:
            unresolved.append({'text': text, 'start': 0, 'end': len(text), 'reason': 'unknown_identity_preserved'})
        return explicit_anatomy({'parser_version': PARSER_VERSION, 'original_text': text,
                'main_ingredient': head['identity'] if len(foods) == 1 and head['polarity'] == 'asserted' else None,
                'foods': foods, 'modifiers': modifiers, 'facets': facets, 'context': context,
                'evidence': evidence, 'unresolved': unresolved,
                'review_reasons': sorted(set(reasons)),
                'grammar_status': 'partial_or_unresolved' if reasons else 'supported_pattern_proposal',
                'acceptance_status': 'proposal_only', 'production_eligible': False,
                'provenance': self.provenance})
