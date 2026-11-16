"""Explainable, provisional ordering of already assessed composition candidates."""
from __future__ import annotations

from recipe_wrangler.matching.normalization import normalized_value


VERSION = 'compatibility_reranker_v4_product_style_development'
CONTEXT_FIELDS = {'identity', 'base_identity', 'compound', 'size', 'size_target', 'thickness'}
QUALIFIERS = {'added_fat', 'product_style', 'added_salt', 'cooking_medium_added_salt',
              'contains_egg', 'pasta_shape', 'coating', 'flavours', 'flavour_status',
              'added_ingredients', 'excluded_flavours', 'excluded_ingredients'}
HANDLING = {'chopped', 'sliced', 'diced', 'grated', 'drained', 'rinsed'}
PRODUCT_PROPERTIES = {'physical_form', 'derived_substance', 'reconstitution_state', 'qualifiers.coating'}
IDENTITY_PROPERTIES = {'qualifiers.flavours', 'qualifiers.flavour_status', 'qualifiers.added_ingredients', 'qualifiers.product_style'}


def properties(annotation: dict) -> dict:
    values = {}
    for field, value in annotation['facets'].items():
        if field in CONTEXT_FIELDS or value is None or value == 'unknown':
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if field != 'processes' or item not in HANDLING:
                    values[(field, normalized_value(field, item))] = True
        else:
            values[(field, normalized_value(field, value))] = True
    for field in QUALIFIERS:
        value = annotation.get('qualifiers', {}).get(field)
        if value is not None:
            for item in value if isinstance(value, (list, tuple, set, frozenset)) else [value]:
                values[('qualifiers.' + field, normalized_value(field, item))] = True
    return values


def ranking_details(query: dict, candidate: dict, compatibility: dict) -> dict:
    """Rank evidence support, never turn missing information into acceptance."""
    query_properties, candidate_properties = set(properties(query)), set(properties(candidate))
    known_identity = bool(query['main_ingredient']) and len(query['foods']) == 1
    identity_matches = known_identity and normalized_value('identity', query['main_ingredient']) == normalized_value('identity', candidate['main_ingredient'])
    additional = candidate_properties - query_properties
    product_extras = {p for p in additional if p[0] in PRODUCT_PROPERTIES}
    starting_form = query.get('qualifiers', {}).get('starting_physical_form')
    if starting_form:
        # Ranking relevance only: a starting powder does not establish the final
        # reconstituted form or authorize a dried-powder nutrient match.
        product_extras.discard(('physical_form', starting_form))
    # With no interpretable query identity, retain retrieval ordering rather than
    # rewarding whichever unrelated suggestion happens to have fewer annotations.
    return {'version': VERSION, 'compatibility': compatibility['relation'],
        'exact_description': any(r['rule_id'] in {'description.exact_full_match', 'description.catalogue_name_match'} for r in compatibility.get('rules', [])),
        'query_identity_known': known_identity, 'identity_matches': bool(identity_matches),
        'unresolved_head_prefix_words': sorted({u['text'] for u in candidate.get('unresolved', [])
            if candidate.get('foods') and 'end' in u and 'start' in candidate['foods'][0]
            and u['end'] <= candidate['foods'][0]['start']}) if known_identity else [],
        'missing_query_properties': sorted(query_properties - candidate_properties) if known_identity else [],
        'missing_query_identity_properties': sorted(p for p in query_properties - candidate_properties if p[0] in IDENTITY_PROPERTIES) if known_identity else [],
        'additional_identity_properties': sorted(p for p in additional if p[0] in IDENTITY_PROPERTIES) if known_identity else [],
        'additional_candidate_properties': sorted(candidate_properties - query_properties) if known_identity else [],
        'additional_product_properties': sorted(product_extras) if known_identity else [],
        'matched_properties': sorted(query_properties & candidate_properties) if known_identity else [],
        'unresolved_candidate_words': sorted({normalized_value('text', u['text']) for u in candidate.get('unresolved', [])}) if known_identity else [],
        'candidate_review_reasons': sorted(set(candidate.get('review_reasons', []))) if known_identity else []}


def ranking_key(details: dict, retrieval_rank: int) -> tuple:
    """Lexicographic policy: compatibility, identity, coverage, specificity, ties."""
    return ({'compatible_proposal': 0, 'needs_review': 1, 'reject': 2}[details['compatibility']],
        0 if details.get('exact_description') else 1,
        0 if not details['query_identity_known'] or details['identity_matches'] else 1,
        len(details.get('unresolved_head_prefix_words', [])),
        len(details['missing_query_identity_properties']), len(details['additional_identity_properties']),
        len(details['missing_query_properties']), len(details['additional_product_properties']),
        len(details['unresolved_candidate_words']),
        len(details['additional_candidate_properties']),
        -len(details['matched_properties']), len(details['candidate_review_reasons']), retrieval_rank)


def rerank(query: dict, candidates: list[dict]) -> list[dict]:
    for rank, candidate in enumerate(candidates, 1):
        candidate['retrieval_rank'] = rank
        candidate['ranking'] = ranking_details(query, candidate['annotation'], candidate['compatibility'])
    ordered = sorted(candidates, key=lambda c: ranking_key(c['ranking'], c['retrieval_rank']))
    for rank, candidate in enumerate(ordered, 1):
        candidate['rerank_position'] = rank
    return ordered
