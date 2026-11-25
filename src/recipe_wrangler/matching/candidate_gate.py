"""Offline compatibility gate around production-compatible retrieval interfaces."""
from __future__ import annotations

import copy
import re
from dataclasses import asdict
from recipe_wrangler.matching.alternatives import select_first_alternative

from recipe_wrangler.matching.hybrid import assess_compatibility, catalogue_name_match
from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.source_suitability import unsupported_specialist_part, material_scope_conflicts
from recipe_wrangler.matching.ingredient_dictionary import normalize_label
from recipe_wrangler.matching.anatomy import explicit_anatomy


class CompatibilityGatedCatalogue:
    """Remove demonstrated contradictions before ranking, including alias lookups.

    Passing this gate does not establish acceptance or nutrient suitability.
    Unknown properties retain the existing provisional calculation behavior.
    """

    def __init__(self, catalogue, annotator, aliases=(), alias_normalizer=None, source_policy=None, facet_retrieval=False):
        self.facet_retrieval = facet_retrieval
        self.catalogue, self.annotator = catalogue, annotator
        self.annotations, self.audit = {}, []
        self.input_name = None
        self.observed_facts = None
        self.errors = catalogue.errors
        self.aliases = {normalize_label(r['alias']):r['food_name'] for r in aliases if r.get('food_name')}
        self.alias_normalizer = alias_normalizer or (lambda name:name)
        self.source_policy = source_policy
        if source_policy is not None:
            self.annotations = source_policy.annotations

    def annotate(self, text):
        if text not in self.annotations:
            self.annotations[text] = self.annotator.annotate(text)
        self.annotations[text] = explicit_anatomy(self.annotations[text])
        return self.annotations[text]

    def begin_input(self, name, observed_facts=None):
        if observed_facts is not None and observed_facts.get('input_text') != name:
            raise ValueError('Occurrence facts must belong to the retrieval name')
        self.input_name = name
        self.observed_facts = copy.deepcopy(observed_facts)

    def semantic_input(self):
        """Use the matcher's first-listed branch without borrowing other options' facts."""
        choice=select_first_alternative(self.input_name)
        if choice is None:
            return self.observed_facts or self.annotate(self.input_name)
        selected=copy.deepcopy(self.annotate(choice.selected))
        selected['inherited_qualifiers']=[]
        return selected

    def _check(self, collection, candidate):
        if self.input_name is None:
            raise ValueError('Ingredient name must be set before gating retrieval')
        query = self.semantic_input()
        semantic_name=query['input_text']
        alias = (self.aliases.get(normalize_label(semantic_name))
                 or self.aliases.get(normalize_label(self.alias_normalizer(semantic_name))))
        if alias:
            # An alias can supply an unknown head (ice -> water), but cannot
            # overwrite a positively identified food or its requested properties.
            alias_identity = self.annotate(alias)['main_ingredient']
            if alias_identity and not query['main_ingredient']:
                query = copy.deepcopy(query)
                query['main_ingredient'] = alias_identity
        label = candidate.get('metadata', {}).get('food_name') or candidate.get('document') or ''
        annotation = self.annotate(label)
        assessment = asdict(assess_compatibility(query, annotation))
        rules = [r for r in assessment['rules'] if r['relation'] == 'reject']
        rules.extend({'rule_id':rule,'relation':'reject','query':semantic_name,'candidate':label}
                     for rule in material_scope_conflicts(query,annotation))
        identity = compare_identities(query['main_ingredient'],annotation['main_ingredient'])
        query_labels = {normalize_label(c['label']) for food in query['foods'] for c in food.get('concept_candidates',[])}
        candidate_labels = {normalize_label(c['label']) for food in annotation['foods'] for c in food.get('concept_candidates',[])}
        known_alias = (normalize_label(annotation['main_ingredient'] or '') in query_labels
                       or normalize_label(query['main_ingredient'] or '') in candidate_labels)
        same_source_name = catalogue_name_match(query,annotation,label)
        complete_identity = compare_identities(self.input_name,label).relation in {'exact','synonym','likely_equivalent'}
        unspecified_berries=re.fullmatch(r'(?:(?:fresh|frozen|dried)\s+)?(?:(?:mixed|summer|assorted|other)\s+)?berries',
            semantic_name.strip(),re.I)
        mixture_source=re.search(r'\b(?:mixed|mix|mixture|blend|assorted|summer)\b',label,re.I)
        if unspecified_berries and not mixture_source and not complete_identity:
            rules.append({'rule_id':'identity.unspecified_mixture_not_single_species','relation':'reject',
                'query':semantic_name,'candidate':label})
        named_dish = any('(dish)' in c['label'].casefold() for food in annotation['foods']
            for c in food.get('concept_candidates',[])) and len((annotation['main_ingredient'] or '').split())>1
        if 'partial_identity_token_overlap' in identity.reasons and named_dish and not known_alias and not same_source_name and not complete_identity:
            rules.append({'rule_id':'identity.different_complete_heads','relation':'reject',
                'query':query['main_ingredient'],'candidate':annotation['main_ingredient']})
        alias_name_supported = alias and compare_identities(alias,label).relation in {'exact','synonym','likely_equivalent'}
        requested_components={normalize_label(v) for field in ['flavours','added_ingredients']
            for v in query.get('qualifiers',{}).get(field,[])}
        stated_components={normalize_label(f['identity']) for f in annotation['foods'][1:]}
        requested_component_scope=(len(annotation['foods'])>1 and bool(stated_components)
            and stated_components<=requested_components and compare_identities(query['main_ingredient'],
                annotation['foods'][0]['identity']).relation in {'exact','synonym','likely_equivalent'})
        unsupported_scope = (len(annotation['foods']) > 1
            or 'only_component_food_head_identified' in annotation['review_reasons'])
        if query['main_ingredient'] and unsupported_scope and not alias_name_supported and not requested_component_scope:
            rules.append({'rule_id':'identity.unsupported_candidate_scope','relation':'reject',
                'query':query['main_ingredient'],'candidate':annotation['main_ingredient']})
        # Fermentation changes product identity even when the query says only milk.
        # Missing style evidence is not a contradiction; explicit extra style is.
        requested_style = query.get('qualifiers', {}).get('product_style')
        actual_style = annotation.get('qualifiers', {}).get('product_style')
        if actual_style and actual_style != requested_style:
            rules.append({'rule_id':'identity.unsupported_product_style', 'relation':'reject',
                          'query':requested_style, 'candidate':actual_style})
        if unsupported_specialist_part(semantic_name,label):
            rules.append({'rule_id':'identity.unsupported_specialist_part','relation':'reject',
                          'query':semantic_name,'candidate':label})
        source_check=self.source_policy.check(collection,candidate,query) if self.source_policy else None
        if source_check:
            rules.extend(source_check['blocking_rules'])
        self.audit.append({'input':self.input_name,'collection':collection,'candidate_id':candidate['id'],
            'label':label,'query_identity':query['main_ingredient'],
            'candidate_identity':annotation['main_ingredient'],'blocked':bool(rules),
            'curated_alias_identity_evidence':alias,
            'blocking_rules':rules,'compatibility':assessment})
        if self.observed_facts is not None:
            from recipe_wrangler.matching.occurrence_facts import occurrence_key
            self.audit[-1]['evaluation_key'] = occurrence_key(self.observed_facts)
        if source_check:
            self.audit[-1]['source_check']={k:v for k,v in source_check['source_facts'].items()
                if k not in {'native_nutrient_evidence','text_annotation','native_annotation'}}
        return not rules

    def query(self, collection, query):
        try:
            candidates=self.catalogue.query(collection,query)
            if self.facet_retrieval:
                from recipe_wrangler.matching.candidate_gate import facet_query
                extra=facet_query(self.semantic_input())
                if extra and extra!=query:
                    candidates=candidates+self.catalogue.query(collection,extra)
            seen=set();result=[]
            for candidate in candidates:
                if candidate['id'] in seen:continue
                seen.add(candidate['id'])
                if self._check(collection,candidate):result.append(candidate)
            return result
        except Exception as error:
            self.errors.append({'collection':collection,'query':query,'reason':'gate_failure',
                                'error_type':type(error).__name__})
            raise

    def by_id(self, collection, identifier):
        candidate = self.catalogue.by_id(collection,identifier)
        return candidate if candidate is not None and self._check(collection,candidate) else None

    def nutrient(self, source, identifier):
        return self.catalogue.nutrient(source,identifier)

    def input_facts(self):
        facts=copy.deepcopy(self.observed_facts or self.annotate(self.input_name))
        choice=select_first_alternative(self.input_name)
        if choice is not None:
            facts['original_parser_annotation']=copy.deepcopy(facts)
            facts['main_ingredient']=None
            facts['facets']={k:[] if isinstance(v,list) else None for k,v in facts['facets'].items()}
            facts['alternatives']=[{'text':p,'annotation':copy.deepcopy(self.annotate(p))} for p in choice.alternatives]
        return facts


def facet_query(facts: dict) -> str | None:
    facets=facts['facets'];terms=[]
    if facts.get('occurrence_recovery',{}).get('conflicts'):return None
    if facets.get('grain_refinement')=='wholegrain':terms.append('wholemeal')
    for field in ['preservation_methods','preparation_state','fat_level','sodium_level','packing_medium']:
        value=facets.get(field)
        if not value:continue
        values=value if isinstance(value,list) else [value]
        for v in values:
            if field=='fat_level':terms.append({'low':'low fat','reduced':'reduced fat','fat_free':'fat free'}.get(v,v.replace('_',' ')))
            elif field=='sodium_level':terms.append(v.replace('_',' ')+' salt')
            elif field=='packing_medium':terms.append('in '+v)
            else:terms.append(v.replace('_',' '))
    for field, word in [('skin_status','skin'),('bone_status','bone')]:
        value=facets.get(field)
        if value in {'absent','skinless','boneless'}:terms.append('without '+word)
        elif value in {'present','with_skin','bone_in'}:terms.append('with '+word)
    if not terms:return None
    name=facts['input_text']
    family=next(iter(facts.get('foods',[])),{}).get('family')
    if family=='pasta' and 'pasta' not in name.casefold():terms.append('pasta')
    additions=[v for v in dict.fromkeys(terms) if v.casefold() not in name.casefold()]
    return name+' '+ ' '.join(additions) if additions else None
