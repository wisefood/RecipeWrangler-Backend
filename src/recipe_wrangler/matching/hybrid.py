"""Contextual properties and explicit compatibility for offline hybrid proposals.

All rules are provisional. No score or rule outcome authorizes nutrient selection.
"""
from __future__ import annotations

import copy
from collections import Counter
import re
from dataclasses import dataclass, asdict

from recipe_wrangler.matching.evaluation.annotation_experiment import prepare_phrase
from recipe_wrangler.matching.contextual_parser import ContextualParser, LEADING_MODIFIERS, _COOKING, _PROCESSES, _PRESERVATION
from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.normalization import normalized_value, LIGHT_MEANS_FAT
from recipe_wrangler.matching.reranking import rerank, ranking_details, ranking_key, VERSION as RERANK_VERSION
from recipe_wrangler.matching.product_identity import attach_product_identity


POLICY_VERSION = 'hybrid_compatibility_v9_catalogue_names_development'
HANDLING = {'chopped', 'sliced', 'diced', 'grated'}
MATERIAL = ('biological_source', 'food_part', 'preparation_state', 'physical_form',
            'skin_status', 'grain_refinement', 'fat_level', 'fat_content_description', 'meat_fat_description',
            'fortification', 'reconstitution_state', 'packing_medium', 'derived_substance')
REVIEW_DIFFERENCES = ('bone_status', 'drainage_status', 'sodium_level', 'sugar_level',
                      'cooking_extent', 'ripeness', 'texture', 'variety', 'seed_status', 'colour')


@dataclass(frozen=True)
class CompatibilityAssessment:
    relation: str
    rules: tuple[dict, ...]
    policy_version: str = POLICY_VERSION


class HybridAnnotator:
    """Reuse contextual parsing and add evidence-backed, single-food pilot qualifiers."""

    def __init__(self, parser: ContextualParser, vocabulary: FacetVocabulary | None = None, identity_retriever=None,
                 langual=None):
        self.parser = parser
        self.vocabulary = vocabulary
        self.identity_retriever = identity_retriever
        self.langual = langual

    def annotate(self, text: str) -> dict:
        prepared = prepare_phrase(text)
        result = copy.deepcopy(self.parser.parse(prepared))
        # Composition tables invert names with commas. Retry only unknown heads,
        # preserving character offsets and original evidence; never collapse foods.
        if not result['foods'] and ',' in prepared:
            alternate = self.parser.parse(prepared.replace(',', ' '))
            if len(alternate['foods']) == 1 and alternate['main_ingredient']:
                result = copy.deepcopy(alternate)
                result['original_text'] = prepared
                result['parse_transformation'] = 'commas_as_equal_length_spaces_for_unknown_head'
                for food in result['foods']:
                    food['text'] = prepared[food['start']:food['end']]
                for modifier in result['modifiers']:
                    modifier['quote'] = prepared[modifier['start']:modifier['end']]
                for evidence in result['evidence']:
                    if evidence['field'] == 'main_ingredient':
                        evidence['quote'] = result['foods'][0]['text']
        if not result['foods'] and self.identity_retriever is not None:
            hypothesis = self.identity_retriever.propose(prepared)
            result['identity_retrieval'] = hypothesis
            if hypothesis['identity_proposal']:
                identity=hypothesis['identity_proposal']
                result['foods']=[{'food_id':'food:0','text':prepared,'start':0,'end':len(prepared),
                    'identity':identity,'family':'generic','family_ambiguous':False,'polarity':'asserted',
                    'scope_start':0,'scope_end':len(prepared),'lexicalized_name':True,
                    'concept_candidates':hypothesis['supporting_concepts']}]
                result['main_ingredient']=identity
                result['facets']['base_identity']=identity
                result['unresolved']=[]
                result['review_reasons']=[r for r in result['review_reasons'] if r not in {'unknown_food_head','unexplained_text'}]
                result['evidence'].append({'field':'main_ingredient','quote':prepared,'polarity':'asserted',
                    'rule':'vector_identity_with_complete_lexical_support',
                    'concept_links':hypothesis['supporting_concepts']})
        if self.vocabulary is not None:
            self.vocabulary.attach_source_part(result)
            self.vocabulary.link_processes(result)
            if self.langual is not None:
                self.langual.attach(result, self.vocabulary)
        result['input_text'] = text
        result['parsed_text'] = prepared
        result['qualifiers'] = {}
        result['annotation_version'] = POLICY_VERSION
        # Explicit meat-fat wording remains available even in an alternative phrase.
        if re.search(r'\b(?:beef|lamb|pork|veal|mutton|chicken|turkey|duck|goose)\b', prepared, re.I):
            match=re.search(r'\b(?:extra[- ]lean|lean and fat|lean)\b',prepared,re.I)
            if (match and 'conflicting_assertions:meat_fat_description' not in result['review_reasons']
                    and not re.search(r'\b(?:not|no|without)\s*$',prepared[:match.start()],re.I)):
                value=match.group().lower().replace('-', ' ')
                result['facets']['meat_fat_description']=value
                result['evidence'].append({'field':'facets.meat_fat_description','quote':match.group(),
                    'polarity':'asserted','rule':'explicit_meat_fat_wording'})
        if len(result['foods']) != 1 or result['main_ingredient'] is None:
            return result
        head = result['foods'][0]
        family = head['family']

        def assert_value(field, value, match, rule):
            prefix = prepared[max(0, match.start()-25):match.start()]
            if re.search(r'\b(?:not|never|for|without|no)\s*$', prefix, re.I):
                return
            target = result['qualifiers'] if field.startswith('qualifiers.') else result['facets']
            key = field.split('.')[-1]
            value = normalized_value(key, value)
            previous = normalized_value(key, target.get(key))
            if previous is not None and previous != value:
                result['review_reasons'].append('conflicting_assertions:' + field)
                return
            target[key] = value
            result['modifiers'].append({'target_food_id': head['food_id'], 'field': field,
                'value': value, 'polarity': 'asserted', 'quote': match.group(),
                'start': match.start(), 'end': match.end(), 'rule': rule})
            result['evidence'].append({'field': field if field.startswith('qualifiers.') else 'facets.'+field,
                                      'quote': match.group(), 'polarity': 'asserted', 'rule': rule})
            result['unresolved'] = [u for u in result['unresolved']
                                    if not (match.start() <= u['start'] and u['end'] <= match.end())]

        if family in {'cereal', 'pasta'}:
            for match in re.finditer(r'\b(?:wholemeal|wholegrain|whole[- ]wheat)\b', prepared, re.I):
                assert_value('grain_refinement', 'wholegrain', match, 'explicit_wholegrain_product')
        for match in re.finditer(r'\bin (?:light |heavy )?syrup\b', prepared, re.I):
            assert_value('packing_medium', 'syrup', match, 'explicit_syrup_packing')
        if re.search(r'\bbananas?\b', prepared, re.I):
            for match in re.finditer(r'\bred\b', prepared, re.I):
                assert_value('variety', 'red_banana', match, 'explicit_banana_variety')
        for value, pattern in [('surimi', r'\bsurimi\b'),
                               ('spoon_dumpling', r'\bspoon dumplings?\b')]:
            for match in re.finditer(pattern, prepared, re.I):
                assert_value('qualifiers.product_style', value, match, 'explicit_product_subtype')
        if family == 'milk':
            for match in re.finditer(r'\bcultured\b', prepared, re.I):
                assert_value('qualifiers.product_style', 'cultured', match, 'explicit_cultured_milk_product')
        if family == 'egg':
            if result['facets']['physical_form'] == 'whole':
                match = re.search(r'\bwhole\b', prepared, re.I)
                assert_value('food_part', 'whole_egg', match, 'egg_whole_edible_portion')
                result['facets']['physical_form'] = None
            for match in re.finditer(r'\bhen(?:s)?\b', prepared, re.I):
                assert_value('biological_source', 'chicken', match, 'hen_is_chicken_egg_source')
        for item in list(result['modifiers']):
            if item['field'] == 'product_style' and item['polarity'] == 'asserted':
                result['qualifiers']['product_style'] = item['value']
        if result['facets']['size_target'] == 'piece':
            for match in re.finditer(r'\bslices?\b', prepared[:head['start']], re.I):
                assert_value('qualifiers.portion_unit', 'slice', match, 'explicit_portion_not_food_identity')
        if head:
            for value, pattern in [('semi_skimmed', r'\b(?:semi[- ]|partly |partially )skimmed\b'),
                                   ('skimmed', r'(?<!semi )(?<!semi-)(?<!partly )(?<!partially )\bskimmed\b'),
                                   ('low', r'\blow[- ]?fat\b'),
                                   ('reduced', r'\b(?:reduced|half)[- ]fat\b'),
                                   *([('reduced', r'\b(?:light|lite)\b')] if LIGHT_MEANS_FAT.search(prepared) else []),
                                   ('fat_free', r'\bfat[- ]free\b'),
                                   ('whole', r'\b(?:whole[- ]fat|full[- ]fat|whole)\b')]:
                for match in re.finditer(pattern, prepared, re.I):
                    if family == 'milk' or value in {'low', 'reduced', 'fat_free'}:
                        assert_value('fat_level', value, match, 'explicit_fat_class')
            for match in re.finditer(r'\b(?:fat\s*)?\d+(?:[.,]\d+)?\s*%(?:\s*fat\b)?', prepared, re.I):
                if family != 'milk' and not re.search(r'\bfat\b', match.group(), re.I):
                    continue
                assert_value('fat_content_description', match.group(), match, 'explicit_numeric_fat')
        for field, value, pattern in [
            ('qualifiers.coating', 'breaded', r'\bbreaded\b'),
            ('qualifiers.coating', 'battered', r'\b(?:battered|in batter)\b'),
            ('reconstitution_state', 'reconstituted', r'\b(?:reconstituted|dissolved)\b'),
            ('fortification', 'unfortified', r'\bunfortified\b'),
            ('fortification', 'fortified', r'\bfortified\b'),
            ('sugar_level', 'no_added_sugar', r'\bno[- ]added[- ]sugar\b'),
            ('sugar_level', 'sugar_free', r'\bsugar[- ]free\b'),
            ('sodium_level', 'low', r'\blow[- ](?:salt|sodium)\b'),
            ('sodium_level', 'reduced', r'\breduced[- ](?:salt|sodium)\b'),
            ('sodium_level', 'no_added_salt', r'\b(?:no[- ]added[- ]salt|without (?:added )?salt)\b'),
        ]:
            for match in re.finditer(pattern, prepared, re.I):
                assert_value(field, value, match, 'explicit_single_food_qualifier')
        if result['facets']['reconstitution_state'] == 'reconstituted' and result['facets']['physical_form'] == 'powder':
            result['qualifiers']['starting_physical_form'] = 'powder'
            result['facets']['physical_form'] = None
            for event in result['modifiers']:
                if event['field'] == 'physical_form' and event['value'] == 'powder':
                    event['field'] = 'qualifiers.starting_physical_form'
                    event['rule'] = 'reconstituted_starting_material_form'
            for evidence in result['evidence']:
                if evidence['field'] == 'facets.physical_form':
                    evidence['field'] = 'qualifiers.starting_physical_form'
                    evidence['rule'] = 'reconstituted_starting_material_form'
            result['review_reasons'].append('reconstituted_final_form_unspecified')
        for value, pattern in [(True, r'\bwith (?:added )?salt\b'),
                               (False, r'\b(?:without (?:added )?salt|no[- ]added[- ]salt)\b')]:
            for match in re.finditer(pattern, prepared, re.I):
                assert_value('qualifiers.added_salt', value, match, 'explicit_salt_addition')
        for match in re.finditer(r'\bin (?:(un)?salted )?water\b', prepared, re.I):
            if result['facets']['preparation_state'] != 'cooked':
                continue
            assert_value('qualifiers.cooking_medium', 'water', match, 'explicit_cooking_medium')
            if 'salted' in match.group().casefold():
                assert_value('qualifiers.cooking_medium_added_salt', not bool(match[1]), match, 'cooking_water_salt')
                result['facets']['preservation_methods'] = [v for v in result['facets']['preservation_methods'] if v != 'salted']
            result['review_reasons'] = [r for r in result['review_reasons'] if r != 'packing_or_cooking_medium_unresolved']
        if family == 'pasta':
            if len(head['text'].split()) == 1 and head['text'].casefold() != 'pasta':
                match = re.compile(re.escape(head['text']), re.I).search(prepared, head['start'])
                assert_value('qualifiers.pasta_shape', head['text'], match, 'foodon_pasta_head_subtype')
            for match in re.finditer(r'\b(?:without|no) egg\b', prepared, re.I):
                assert_value('qualifiers.contains_egg', False, match, 'explicit_egg_exclusion')
            for match in re.finditer(r'\begg\b', prepared, re.I):
                assert_value('qualifiers.contains_egg', True, match, 'pasta_ingredient_not_animal_source')
            for match in re.finditer(r'\bwhite\b', prepared, re.I):
                assert_value('colour', 'white', match, 'explicit_pasta_colour')
            for unresolved in list(result['unresolved']):
                hits = self.parser._lookup(unresolved['text'])
                if unresolved['text'].casefold() == 'pasta':
                    result['unresolved'].remove(unresolved)
                    continue
                if hits and any('pasta' in self.parser.families.get(h.term_id, set()) for h in hits):
                    match = re.compile(re.escape(unresolved['text']), re.I).search(prepared, unresolved['start'])
                    assert_value('qualifiers.pasta_shape', unresolved['text'], match, 'foodon_pasta_subtype')
        if head['identity'] in {'sweetcorn', 'corn', 'maize'}:
            for match in re.finditer(r'\bkernels?\b', prepared, re.I):
                assert_value('food_part', 'kernel', match, 'explicit_maize_kernel')
        if 'fried' in result['facets']['processes']:
            for value, pattern in [('none', r'\bwithout (?:added )?fat\b'),
                ('butter', r'\bin butter\b'), ('margarine', r'\bin margarine\b'),
                ('sunflower_oil', r'\bin sunflower oil\b')]:
                for match in re.finditer(pattern, prepared, re.I):
                    assert_value('qualifiers.added_fat', value, match, 'actual_frying_added_fat')
        if not result['unresolved']:
            result['review_reasons'] = [r for r in result['review_reasons'] if r != 'unexplained_text']
        attach_product_identity(result, self.parser)
        result['review_reasons'] = sorted(set(result['review_reasons']))
        return result


def assess_compatibility(query: dict, candidate: dict, *, exact_description: bool = False, catalogue_name: bool = False) -> CompatibilityAssessment:
    """Compare scoped assertions; missing information never establishes agreement."""
    rules = []

    def record(rule, relation, left=None, right=None):
        rules.append({'rule_id': rule, 'relation': relation, 'query': left, 'candidate': right})

    a, b = query['facets'], candidate['facets']
    qid, cid = query['main_ingredient'], candidate['main_ingredient']
    if not qid or not cid:
        record('identity.unresolved_or_multiple', 'needs_review', qid, cid)
    elif qid == cid:
        record('identity.same_head', 'agree', qid, cid)
    else:
        identity = compare_identities(qid, cid)
        qconcepts={c['term_id'] for f in query['foods'] for c in f.get('concept_candidates',[])}
        cconcepts={c['term_id'] for f in candidate['foods'] for c in f.get('concept_candidates',[])}
        product_terms = {'paste', 'juice', 'stock', 'sauce'}
        different_product_form = bool((set(qid.split()) ^ set(cid.split())) & product_terms)
        if len(qconcepts)==1 and qconcepts==cconcepts and not different_product_form:
            record('identity.same_exact_dictionary_concept','agree',qid,cid)
        else:
            record('identity.distinct_products', 'reject' if identity.relation == 'different' or different_product_form else 'needs_review', qid, cid)

    # A qualitative reduced-fat request never establishes equivalence to fat-free milk.
    for left, right in ((a, b), (b, a)):
        if (normalized_value('fat_level', left.get('fat_level')) in {'low', 'reduced'}
                and normalized_value('fat_content_description', right.get('fat_content_description')) == '0% fat'):
            record('property.low_fat_is_not_fat_free', 'reject', left['fat_level'], '0% fat')

    # Explicit exclusions can reject even when other input details remain uncertain.
    for item in query['modifiers']:
        if item['polarity'] != 'negated' or item['target_food_id'] is None:
            continue
        actual = b.get(item['field'])
        if actual == item['value'] or isinstance(actual, list) and item['value'] in actual:
            record('polarity.explicit_exclusion:' + item['field'], 'reject', item['value'], actual)
    alternatives = {m['value'] for m in query['modifiers'] if m['field'] == 'processes'
                    and m['polarity'] == 'alternative' and m['target_food_id'] is not None}
    actual_cooking = set(b['processes']) & set(_COOKING)
    if alternatives and actual_cooking and not alternatives.intersection(actual_cooking):
        record('polarity.process_outside_alternatives', 'reject', sorted(alternatives), sorted(actual_cooking))

    for field in MATERIAL + REVIEW_DIFFERENCES:
        left, right = normalized_value(field, a.get(field)), normalized_value(field, b.get(field))
        left = None if left == 'unknown' else left
        right = None if right == 'unknown' else right
        if exact_description and field == 'food_part' and right == 'meat':  # 'Chicken meat, breast ...': 'meat' is the substance, not a cut that contradicts 'breast'
            right = None
        if left is None and right is None:
            continue
        relation = ('needs_review' if left is None or right is None else 'agree' if left == right
                    else 'needs_review' if field in REVIEW_DIFFERENCES or (
                        field == 'fat_level' and {left, right} in ({'low', 'semi_skimmed'},{'low','reduced'})) else 'reject')
        record('property.' + field, relation, left, right)
    for field in ['added_fat', 'product_style', 'added_salt', 'cooking_medium',
                  'cooking_medium_added_salt', 'contains_egg', 'pasta_shape', 'coating']:
        left, right = query.get('qualifiers', {}).get(field), candidate.get('qualifiers', {}).get(field)
        if field == 'pasta_shape':
            if left is not None or right is not None:
                record('qualifier.pasta_shape_retained_not_nutrient_constraint', 'agree', left, right)
            continue
        # Ordinary water is retained as evidence, not an extra matching requirement.
        # Salt and other media still have their own compatibility checks.
        if field == 'cooking_medium' and left in {None, 'water'} and right in {None, 'water'}:
            continue
        if left is not None or right is not None:
            record('qualifier.' + field, 'agree' if left == right else 'needs_review' if left is None or right is None
                   else 'reject' if field in {'added_fat', 'added_salt', 'cooking_medium_added_salt', 'contains_egg'} else 'needs_review', left, right)
    for field in ['flavours', 'flavour_status', 'added_ingredients']:
        left, right = set(query.get('qualifiers', {}).get(field, [])), set(candidate.get('qualifiers', {}).get(field, []))
        if left or right:
            contradiction = (field == 'flavours' and left and right and left.isdisjoint(right)) or (
                field == 'flavour_status' and ('unflavoured' in left and 'flavoured' in right or 'flavoured' in left and 'unflavoured' in right))
            relation = 'agree' if left == right else 'reject' if contradiction else 'needs_review'
            record('product.' + field, relation, sorted(left), sorted(right))
    for excluded, present in [('excluded_flavours', 'flavours'), ('excluded_ingredients', 'added_ingredients')]:
        requested = set(query.get('qualifiers', {}).get(excluded, []))
        overlap = requested & set(candidate.get('qualifiers', {}).get(present, []))
        if overlap:
            record('product.explicit_exclusion:' + present, 'reject', sorted(overlap), sorted(overlap))
        elif requested:
            supported = set(candidate.get('qualifiers', {}).get(excluded, []))
            record('product.exclusion_evidence:' + present, 'agree' if requested <= supported else 'needs_review',
                   sorted(requested), sorted(supported))
    if (a.get('food_part') and len(query['foods']) == 1 and query['foods'][0]['family'] == 'meat'
            and not query.get('qualifiers', {}).get('coating') and candidate.get('qualifiers', {}).get('coating')):
        record('identity.anatomical_cut_vs_coated_product', 'reject', qid, cid)
    for field in ['processes', 'preservation_methods']:
        left, right = set(a[field]), set(b[field])
        if field == 'processes':
            left, right = left - HANDLING, right - HANDLING
            if 'drained' in left or 'drained' in right or 'rinsed' in left or 'rinsed' in right:
                left, right = left-{'drained','rinsed'}, right-{'drained','rinsed'}
        if not left and not right:
            continue
        relation = 'agree' if left == right else 'needs_review'
        if left and right and left != right:
            if field == 'processes' or ('dried' in left) != ('dried' in right):
                relation = 'reject'
        record('property.' + field, relation, sorted(left), sorted(right))
    for side, annotation in [('query', query), ('candidate', candidate)]:
        # Multiple ontology candidates within one understood family are link uncertainty,
        # not automatically ambiguity of food identity.
        for reason in annotation['review_reasons']:
            if reason != 'ambiguous_ontology_candidates':
                record(side + '.interpretation:' + reason, 'needs_review')
    for annotation, side in [(query, 'query'), (candidate, 'candidate')]:
        if len(annotation['foods']) == 1 and annotation['foods'][0]['family'] in {'egg', 'meat'}:
            for field in ['preparation_state', 'biological_source']:
                if annotation['facets'][field] in (None, 'unknown'):
                    record(side + '.required_unknown:' + field, 'needs_review')
            if annotation['foods'][0]['family'] == 'egg' and annotation['facets']['food_part'] is None:
                record(side + '.required_unknown:egg_portion', 'needs_review')
    # Literal wording can cover parser omissions, never contradictory assertions,
    # ambiguous scope, native issues or unresolved product-basis questions.
    if (exact_description and qid and qid == cid and len(query['foods']) == len(candidate['foods']) == 1
            and not any(m['polarity'] in {'alternative', 'intended'} for m in query['modifiers'])):
        def omission(rule):
            name = rule['rule_id']
            return ('.required_unknown:' in name or name.endswith('.interpretation:unexplained_text')
                    or name.startswith(('property.', 'qualifier.'))
                    and (rule['query'] in (None, [], '') or rule['candidate'] in (None, [], '')))
        blockers = [r for r in rules if r['relation'] != 'agree' and not (
            r['relation'] == 'needs_review' and omission(r))]
        if not blockers:
            for rule in rules:
                if rule['relation'] == 'needs_review':
                    rule['original_relation'] = rule['relation']
                    rule['relation'] = 'covered_by_exact_description'
            record('description.exact_full_match', 'agree')
    # A composition record's complete name is direct identity evidence even when
    # FoodOn has no entry. This never applies to a vector-only suggestion.
    if catalogue_name and not any(r['rule_id'] == 'description.exact_full_match' for r in rules):
        def name_omission(rule):
            name = rule['rule_id']
            return (name == 'identity.unresolved_or_multiple'
                    or '.required_unknown:' in name
                    or name.endswith(('.interpretation:unexplained_text', '.interpretation:unknown_food_head'))
                    or name.startswith(('property.', 'qualifier.'))
                    and rule['relation'] == 'needs_review'
                    and (rule['query'] in (None, [], '') or rule['candidate'] in (None, [], '')))
        if not any(r['relation'] not in {'agree', 'covered_by_exact_description'} and not (
                r['relation'] == 'needs_review' and name_omission(r)) for r in rules):
            for rule in rules:
                if rule['relation'] == 'needs_review':
                    rule['original_relation'] = rule['relation']
                    rule['relation'] = 'covered_by_catalogue_name'
            record('description.catalogue_name_match', 'agree')
    relation = 'reject' if any(r['relation']=='reject' for r in rules) else 'needs_review' if any(
        r['relation']=='needs_review' for r in rules) else 'compatible_proposal'
    return CompatibilityAssessment(relation, tuple(rules))


def exact_description_match(left: str, right: str) -> bool:
    """Preserve word order, numbers, negation and meaningful punctuation."""
    def normalize(text):
        return re.sub(r'\s*([,;])\s*', r'\1', normalized_value('text', text)).strip(' .')
    return bool(normalize(left)) and normalize(left) == normalize(right)


def catalogue_name_match(query: dict, candidate: dict, label: str) -> bool:
    """Complete source wording only; reordering requires a shared parsed head."""
    from recipe_wrangler.matching.ingredient_dictionary import normalize_label
    text = query.get('parsed_text', query.get('original_text', ''))
    if not text or any(len(a['foods']) > 1 for a in (query, candidate)):
        return False
    # Context, alternatives and attachment grammar cannot be reduced to a bag of words.
    unsafe = r'\b(?:not|no|without|with|in|or|and|for|contains?|containing|flavou?red|from|of|to)\b|[/+]'
    if re.search(unsafe, text + ' ' + label, re.I):
        return False
    def tokens(value):
        return [normalize_label(w) if not w[0].isdigit() else w.replace(',', '.')
                for w in re.findall(r'\d+(?:[.,]\d+)?|[^\W\d_]+|[%()]', value.casefold())]
    left, right = tokens(text), tokens(label)
    if left == right:
        return bool(left)
    if Counter(left) != Counter(right):
        return False
    if query['main_ingredient'] and query['main_ingredient'] == candidate['main_ingredient']:
        return True
    # Move understood process/preservation words only; unknown food-name words
    # retain their order (milk chocolate must not become chocolate milk).
    def food_words(value):
        for pattern in (*_PROCESSES.values(), *_PRESERVATION.values()):
            value = re.sub(pattern, ' ', value, flags=re.I)
        return tokens(value)
    return bool(food_words(text)) and food_words(text) == food_words(label)


class HybridMatcher:
    """Assess externally retrieved vectors/exact hits without accepting nutrient records."""

    def __init__(self, annotator: HybridAnnotator, composition_annotations: dict | None = None):
        self.annotator = annotator
        self.cache = {}
        self.composition_annotations = composition_annotations

    def annotate(self, text: str) -> dict:
        if text not in self.cache:
            self.cache[text] = self.annotator.annotate(text)
        return self.cache[text]

    def match(self, text: str, candidates: list[dict]) -> dict:
        query = self.annotate(text)
        assessments = []
        for hit in candidates:
            candidate = self.annotate(hit['label'])
            exact = exact_description_match(text, hit['label'])
            assessed = assess_compatibility(query, candidate, exact_description=exact)
            source_assessments = []
            if self.composition_annotations is not None:
                stored_variants = [self.composition_annotations.get(s['catalogue_key']) for s in hit.get('records', [])]
                exact_variants = [s for s in stored_variants if s and (
                    exact_description_match(text, s.get('label', ''))
                    or catalogue_name_match(query, s['text_annotation'], s.get('label', '')))]
                native_conflict = False
                for field in MATERIAL + REVIEW_DIFFERENCES + ('processes', 'preservation_methods'):
                    values = [normalized_value(field, s.get('native_facets', {}).get(field)) for s in exact_variants]
                    known = [v for v in values if v not in (None, [], '', 'unknown')]
                    native_conflict |= query['facets'].get(field) in (None, [], '', 'unknown') and any(v != known[0] for v in known[1:])
                for source in hit.get('records', []):
                    stored = self.composition_annotations.get(source['catalogue_key'])
                    if stored is None:
                        source_assessments.append({'catalogue_key': source['catalogue_key'],
                            'compatibility': {'relation': 'needs_review', 'rules': [
                                {'rule_id': 'source.annotation_missing', 'relation': 'needs_review'}]}})
                        continue
                    annotated = copy.deepcopy(stored['text_annotation'])
                    annotated['facets'] = copy.deepcopy(stored['facets'])
                    annotated['review_reasons'].extend('native:' + issue for issue in stored['issues'])
                    if native_conflict and (exact_description_match(text, stored.get('label', ''))
                            or catalogue_name_match(query, annotated, stored.get('label', ''))):
                        annotated['review_reasons'].append('exact_description_native_variants_conflict')
                    metadata = stored.get('metadata', {})
                    if (query['facets'].get('derived_substance') == 'broth_or_stock'
                            and not (query['facets'].get('physical_form') in {'cube', 'powder', 'paste', 'granules'}
                                     and query['facets']['physical_form'] == annotated['facets'].get('physical_form'))):
                        annotated['review_reasons'].append('stock_dilution_concentration_unverified')
                    if query['main_ingredient'] == 'tomato paste' and 'puree' in str(metadata.get('FoodEx2 description', '')).casefold():
                        annotated['review_reasons'].append('paste_puree_concentration_unverified')
                    source_assessments.append({'catalogue_key': source['catalogue_key'],
                        'annotation': annotated, 'native_evidence': stored['native_evidence'],
                        'compatibility': asdict(assess_compatibility(query, annotated, exact_description=not native_conflict and exact_description_match(text, stored.get('label', '')), catalogue_name=not native_conflict and catalogue_name_match(query, annotated, stored.get('label', ''))))})
                if source_assessments:
                    for source in source_assessments:
                        source['ranking'] = ranking_details(query, source.get('annotation', candidate), source['compatibility'])
                    best = min(enumerate(source_assessments), key=lambda pair: ranking_key(pair[1]['ranking'], pair[0]))[1]
                    candidate, assessed = best.get('annotation', candidate), best['compatibility']
                else:
                    assessed = {'relation': 'needs_review', 'rules': [
                        {'rule_id': 'source.records_missing', 'relation': 'needs_review'}]}
            assessments.append({**hit, 'annotation': candidate, 'source_assessments': source_assessments,
                'compatibility': asdict(assessed) if isinstance(assessed, CompatibilityAssessment) else assessed})
        assessments = rerank(query, assessments)
        eligible = [c for c in assessments if c['compatibility']['relation']=='compatible_proposal']
        selected = eligible[0] if eligible else None
        method = ('catalogue_name' if any(r['rule_id'] == 'description.catalogue_name_match' for r in selected['compatibility']['rules']) else 'exact_description' if any(r['rule_id'] == 'description.exact_full_match' for r in selected['compatibility']['rules']) else 'hybrid') if selected else None
        # Retrieval-only proposals cannot override asserted properties, exclusions,
        # multiple foods or contradictions. They never establish compatibility.
        has_properties = any(v not in (None, [], '') for k, v in query['facets'].items()
            if k not in {'identity', 'base_identity', 'compound', 'size', 'size_target', 'thickness'})
        has_properties |= any(v is not None for k, v in query.get('qualifiers', {}).items()
                              if k != 'portion_unit')
        protected = any(m['polarity'] != 'asserted' for m in query['modifiers']) or bool(query['facets'].get('compound'))
        protected |= any('conflict' in r for r in query['review_reasons'])
        protected |= any(LEADING_MODIFIERS.search(u['text']) for u in query['unresolved'])
        if selected is None and not has_properties and not protected:
            fallback = [c for c in assessments if c.get('retrieval_reason') in {'vector', 'exact_label_and_vector'}
                        and c['compatibility']['relation'] != 'reject'
                        and not any(s['compatibility']['relation'] == 'reject' for s in c['source_assessments'])]
            if fallback:
                selected = fallback[0]
                method = 'exact_label_fallback' if selected.get('retrieval_reason') == 'exact_label_and_vector' else 'embedding_fallback'
        retrieval_only = method in {'embedding_fallback', 'exact_label_fallback'}
        compound = bool(query['facets'].get('compound')) or len(query['foods']) > 1
        review_candidate = next((c for c in assessments if c['compatibility']['relation'] != 'reject'), None)
        return {'input_text': text, 'annotation': query, 'candidates': assessments,
            'proposed_candidate_key': selected['key'] if selected else None,
            'proposed_record_keys': [s['catalogue_key'] for s in selected['source_assessments']
                                     if s['compatibility']['relation'] == 'compatible_proposal'] if selected else [],
            'match_method': method,
            'rerank_version': RERANK_VERSION,
            'review_candidate_key': review_candidate['key'] if review_candidate and not selected and not compound else None,
            'food_components': [{'food_id': f['food_id'], 'identity': f['identity']} for f in query['foods']] if compound else [],
            'facet_validation': 'unverified' if retrieval_only else 'provisional_checks_passed' if selected else 'not_passed',
            'notes': ['No query facets matched; retrieval-only candidate. Compatibility unverified.'] if retrieval_only else ['Complete catalogue name match; identity/facet extraction may be incomplete.'] if method in {'exact_description', 'catalogue_name'} else [],
            'status': 'fallback_proposal' if retrieval_only else 'proposal' if selected else 'needs_review' if candidates else 'unresolved',
            'policy_version': POLICY_VERSION, 'production_eligible': False,
            'limitations': ['Annotations and policies are provisional.', 'Nutrient eligibility and source selection are not assessed.']}
