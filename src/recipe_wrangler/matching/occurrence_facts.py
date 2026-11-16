"""Recover grounded occurrence properties without changing the retrieval name."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable

from recipe_wrangler.matching.contextual_parser import _COOKING
from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.alternatives import selected_source_branch


VERSION = 'occurrence_facts_v2_first_source_branch_development'
FIELDS = ('preparation_state', 'processes', 'preservation_methods', 'physical_form',
          'food_part', 'skin_status', 'bone_status', 'packing_medium', 'drainage_status',
          'fat_level', 'fat_content_description', 'meat_fat_description', 'sodium_level',
          'sugar_level', 'grain_refinement', 'reconstitution_state', 'fortification')


def source_description_name(name: str, original: str, released_labels: set[str]) -> str:
    """Restore an explicitly supplied catalogue description after a shortened head.

    This only removes a leading numeric g/kg/ml/l quantity; it does not infer
    ingredients, interpret alternatives or use nutrient values to choose food.
    """
    description = re.sub(r'^\s*\d+(?:[.,]\d+)?\s*(?:kg|ml|g|l)\s+', '', original or '', flags=re.I).strip()
    normalize = lambda value: ' '.join(value.casefold().split())
    head, separator, _ = description.partition(',')
    # "and/or" is part of a table label, not an alternative.
    if not separator or re.search(r'(?<!and/)\bor\b|\balternatively\b',description,re.I) or not _head_covers(head, name):
        return name
    if normalize(description) in released_labels:
        return description
    # Workbooks truncate long labels: accept the single released label the stated text starts.
    stated = normalize(description)
    longer = [label for label in released_labels if label.startswith(stated + ' ')]
    return ' '.join(description.split()) + longer[0][len(stated):] if len(longer) == 1 else name


def stated_oil_example(name: str, original: str) -> dict | None:
    """Select only an explicitly named first oil example, never infer its species."""
    if not re.fullmatch(r'\s*unsaturated oil\s*', name, re.I):
        return None
    match = re.search(r'\bunsaturated oil\s+(?:like|such as|e\.g\.?)\s+'
                      r'(rapeseed|sunflower|olive)(?:\s+oil)?\b', original, re.I)
    if not match:
        return None
    return {'name': match[1].casefold() + ' oil', 'quote': match[0],
            'start': match.start(), 'end': match.end()}


def _head_covers(head: str, name: str) -> bool:
    """Equal heads, or a parser that dropped head words ("bananas" from "Banana bread")."""
    from recipe_wrangler.tools.nutrition_match import _singular
    stems = lambda w: {w, _singular(w), w[:-1] if w.endswith('s') else w}
    head_stems = set().union(*map(stems, re.findall(r'[a-z]+', head.casefold())))
    name_words = re.findall(r'[a-z]+', name.casefold())
    return bool(name_words) and all(stems(w) & head_stems for w in name_words)
# shortcut: ambiguous multi-food scope is deferred; extend only with tested attachment rules.
AMBIGUOUS = re.compile(r'\bor\b|\balternatively\b|\bwith\b(?!\s+(?:skin|bones?)\b)|'
                       r'\band\b(?!\s+(?:rinsed|drained|chopped|diced|sliced|skin|bones?|fat)\b)', re.I)


def recover_occurrence_facts(name: str, original: str, annotate: Callable[[str], dict]) -> dict:
    """Add only supported same-food properties; retain conflicts and source spans."""
    result = copy.deepcopy(annotate(name))
    recovery = {'version': VERSION, 'original_text': original, 'recovered': [],
                'conflicts': [], 'deferred_reason': None}
    result['occurrence_recovery'] = recovery
    if not original.strip():
        recovery['deferred_reason'] = 'missing_original_text'
        return result
    selected = selected_source_branch(name, original)
    source_text = selected or original
    if selected:
        recovery['selected_source_text'] = selected
    # Both rice colour alternatives share an explicit preceding state. Select
    # only the colour already named by the parser; do not guess another food.
    rice=re.search(r'\b(cooked|raw|uncooked)\s+(black|brown|white)\s+or\s+(black|brown|white)\s+rice\b',original,re.I)
    if rice and re.fullmatch(r'(?:black|brown|white) rice',name,re.I):
        colour=name.split()[0].casefold()
        if colour in {rice[2].casefold(),rice[3].casefold()}:
            source_text=original[:rice.start()]+rice[1]+' '+colour+' rice'+original[rice.end():]
    if AMBIGUOUS.search(source_text) or AMBIGUOUS.search(name):
        recovery['deferred_reason'] = 'ambiguous_food_or_modifier_scope'
        return result
    source = copy.deepcopy(annotate(source_text))
    # Parenthetical yields/servings and dissolved components have their own
    # state and mass basis; do not project these onto the purchased ingredient.
    if re.search(r'\((?!\s*(?:cooked|raw|uncooked)\s*\))[^)]*\b(?:cooked|raw|per person|per serving)\b|\bdissolv(?:ed|ing)\b', original, re.I):
        recovery['deferred_reason'] = 'component_state_or_quantity_basis_unresolved'
        return result
    identity = compare_identities(result.get('main_ingredient'), source.get('main_ingredient'))
    if (len(source['foods']) != 1 or not source.get('main_ingredient') or
            identity.relation not in {'exact', 'synonym', 'likely_equivalent'}):
        recovery['deferred_reason'] = 'source_identity_not_established'
        return result
    claims = source['facets']
    head = source['foods'][0]
    prepared = source.get('parsed_text', original)
    nominal = [m for m in source['modifiers'] if m['field'] == 'processes'
               and m['end'] <= head['start'] and m['quote'].casefold().endswith('ing')]
    if nominal:
        claims['processes'] = [v for v in claims['processes'] if v not in {m['value'] for m in nominal}]
        if not claims['processes'] and not re.search(r'\b(?:raw|uncooked|cooked)\b', prepared, re.I):
            claims['preparation_state'] = None
    evidence = [e for e in source['evidence'] if e.get('polarity') == 'asserted'
                and e.get('quote') and e['quote'] in original]
    # Container wording supports a provisional canned interpretation, not an
    # observed heat-treatment claim. Keep that distinction in the calculation.
    package = re.search(r'\b(tins?|cans?)\s+(?:of\s+)?$', prepared[:head['start']], re.I)
    if package and not claims.get('preservation_methods'):
        claims['preservation_methods'] = ['canned']
        evidence.append({'field': 'facets.preservation_methods', 'quote': package.group(),
                         'polarity': 'asserted', 'status': 'inferred_from_container'})
    if package and not claims.get('packing_medium'):
        medium = re.search(r'\bin (brine|oil|water)\b', original, re.I)
        if medium:
            claims['packing_medium'] = medium[1].casefold()
            evidence.append({'field': 'facets.packing_medium', 'quote': medium.group(),
                             'polarity': 'asserted'})
    for field in FIELDS:
        value = claims.get(field)
        if value is None or value == []:
            continue
        support = [e for e in evidence if e['field'] == 'facets.' + field]
        if field == 'drainage_status':
            support += [e for e in evidence if e['field'] == 'facets.processes'
                        and re.search(r'\b(?:drained|rinsed)\b', e['quote'], re.I)]
        if field == 'preparation_state' and value == 'cooked':
            support += [e for e in evidence if e['field'] == 'facets.processes'
                        and any(re.search(pattern, e['quote'], re.I) for pattern in _COOKING.values())]
        if not support or any('conflicting' in r and (field in r or 'preparation' in r)
                              for r in source['review_reasons']):
            continue
        if field in {'skin_status', 'bone_status'} and any(
            re.search(r'\b(?:to|not|never)\b[^,;()]*$', source_text[:source_text.find(e['quote'])], re.I)
            for e in support if re.search(r'\bremoved\b', e['quote'], re.I)):
            continue
        # Intended later operations must not become the current ingredient state.
        if any(re.search(r'\b(?:for|to)(?:\s+be)?\s*$', original[:original.find(e['quote'])], re.I)
               for e in support):
            continue
        old = result['facets'].get(field)
        if old == value:
            continue
        supported_extension = isinstance(old, list) and isinstance(value, list) and set(old) <= set(value)
        if old not in (None, []) and not supported_extension:
            recovery['conflicts'].append({'field': field, 'name_value': old, 'source_value': value})
            continue
        assertions = []
        for e in support:
            assertions.append({'quote': e['quote'], 'status': e.get('status', 'stated'),
                               'spans': [{'start': m.start(), 'end': m.end()}
                                         for m in re.finditer(re.escape(e['quote']), original)
                                         if not selected or (m.start() >= original.index(selected)
                                             and m.end() <= original.index(selected) + len(selected))]})
        result['facets'][field] = copy.deepcopy(value)
        recovery['recovered'].append({'field': field, 'value': value, 'evidence': assertions})
    # The name and its text annotation remain intact; source spans refer to the
    # separately stored original line, never to the quantity-free name.
    return result


def occurrence_key(facts: dict) -> str:
    """Share decisions only when names, properties and recovery status agree."""
    recovery = facts['occurrence_recovery']
    payload = {'name': facts['input_text'], 'facets': facts['facets'],
               'conflicts': recovery['conflicts'], 'version': VERSION,
               'inferred': sorted(r['field'] for r in recovery['recovered']
                                  if any(e['status'] != 'stated' for e in r['evidence']))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def occurrence_inputs(pack: dict, annotate: Callable[[str], dict]) -> tuple[list[dict], dict, list[dict]]:
    """Group equivalent decisions while preserving each occurrence's own evidence."""
    jobs, keys, ledger = {}, {}, []
    for recipe in pack['recipes']:
        for occurrence in recipe['occurrences']:
            name = occurrence['parsed_name'].strip()
            facts = recover_occurrence_facts(name, occurrence.get('original_text') or '', annotate)
            key = occurrence_key(facts)
            oid = occurrence['occurrence_id']
            if oid in keys:
                raise ValueError('Duplicate occurrence ID: ' + oid)
            keys[oid] = key
            context = {'recipe_id': recipe['recipe_id'], 'recipe_title': recipe['title'], **occurrence}
            jobs.setdefault(key, {'input': name, 'evaluation_key': key,
                                  'observed_facts': facts, 'contexts': []})['contexts'].append(context)
            ledger.append({'occurrence_id': oid, 'recipe_id': recipe['recipe_id'],
                           'evaluation_key': key, 'input_ingredient': name,
                           'recovery': facts['occurrence_recovery']})
    return list(jobs.values()), keys, ledger
