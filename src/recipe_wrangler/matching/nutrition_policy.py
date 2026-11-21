"""Provisional source checks and explicit calculation decisions for offline runs."""
from __future__ import annotations

import copy
import math
import re

from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS, native_candidate_id
from recipe_wrangler.matching.composition_annotation import annotate_composition
from recipe_wrangler.matching.hybrid import assess_compatibility, catalogue_name_match, exact_description_match
from recipe_wrangler.utils.food_identity import compare_identities

VERSION = 'nutrition_source_and_estimate_v2_occurrence_evidence_development'
METRICS = ('energy_kcal','protein_g','carbs_g','fat_g','sugar_g','saturated_fat_g','sodium_mg','fibre_g')
CORE = METRICS[:4]


def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0


class SourceNutrientPolicy:
    """Resolve exact source keys; never crosswalk legacy IDs into another release."""

    def __init__(self, catalogue, annotator, records, profiles, foodex2, nutrient_reader):
        self.catalogue,self.annotator = catalogue,annotator
        self.records,self.profiles,self.foodex2 = records,profiles,foodex2
        self.nutrient_reader = nutrient_reader
        self.cache = {}
        self.annotations = {}

    def source(self, collection: str, candidate: dict) -> dict:
        from recipe_wrangler.matching.anatomy import explicit_anatomy, source_anatomy
        key=(collection,str(candidate['id']))
        if key in self.cache:
            return copy.deepcopy(source_anatomy(self.cache[key]))
        source=next(s for s,c in COLLECTIONS.items() if c==collection)
        identifier=native_candidate_id(candidate,source)
        row=self.catalogue.nutrient(source,identifier)
        metadata=candidate.get('metadata') or {}
        catalogue_key=metadata.get('catalogue_key')
        text=candidate.get('document') or metadata.get('food_name') or ''
        if text not in self.annotations:
            self.annotations[text]=self.annotator.annotate(text)
        annotation=explicit_anatomy(self.annotations[text])
        blockers=[];warnings=[];native=None;profile=None
        values=dict.fromkeys(METRICS)
        if row is None:
            blockers.append('missing_nutrient_row')
        else:
            row_id=row.get('canonical_food_id') if source in {'irish','hungarian'} else row.get('id')
            if row_id is not None and str(row_id)!=identifier:
                blockers.append('nutrient_row_id_mismatch')
            values=self.nutrient_reader(row)
            values={m:values.get(m) if number(values.get(m)) else None for m in METRICS}
        if catalogue_key:
            record=self.records.get(catalogue_key);profile=self.profiles.get(catalogue_key)
            if record is None or profile is None:
                blockers.append('missing_exact_released_record_or_profile')
            else:
                warnings.extend(record.get('issues',[]))
                if metadata.get('catalogue_source')!=record['source'] or profile['source']!=record['source']:
                    blockers.append('source_release_mismatch')
                if row and (row.get('catalogue_key') not in (None,catalogue_key) or
                    row.get('catalogue_source') not in (None,record['source'])):
                    blockers.append('nutrient_row_release_mismatch')
                if record['label']!=text or profile['label']!=record['label']:
                    blockers.append('source_label_mismatch')
                if row and row.get('food_name') not in (None,record['label']):
                    blockers.append('nutrient_row_label_mismatch')
                native=annotate_composition(record,self.annotator,self.foodex2,text_annotation=annotation)
                warnings.extend(native['issues'])
                for issue in native['issues']:
                    if issue.startswith(('source_label_disagreement:','source_label_negated_process',
                                         'native_physical_form_basis_unspecified')):
                        blockers.append(issue)
                for metric in METRICS:
                    expected=profile['values_per_100g'].get(metric)
                    actual=values.get(metric)
                    if (number(expected) != number(actual) or number(expected) and
                            not math.isclose(expected,actual,rel_tol=1e-9,abs_tol=1e-9)):
                        blockers.append('native_nutrient_linkage_mismatch:'+metric)
                warnings.extend(profile.get('notes',[]))
        if row:
            basis=row.get('reporting_basis') or row.get('basis')
            if basis and basis not in {'per_100g_edible','per 100 g edible food in selected state'}:
                blockers.append('unsupported_reporting_basis:'+str(basis))
        # The existing native reader filters EFSA values to Edible portion and
        # explicit /100 gram units. Legacy snapshots have only their old contract.
        basis_supported=bool(profile and (profile.get('native_values') or
            profile.get('basis')=='per 100 g edible food in selected state'))
        if not basis_supported:
            warnings.append('legacy_per_100g_basis_not_independently_validated')
        warnings.append('carbohydrate_definition_retained_from_source_not_harmonized')
        result={'source_key':source,'native_id':identifier,'catalogue_key':catalogue_key,
            'source':metadata.get('catalogue_source') or {'dataset':metadata.get('source') or source,
                'release':'frozen_legacy_inventory','record_id':identifier},
            'label':text,'text_annotation':annotation,'native_annotation':native,
            'basis':{'amount':100,'unit':'g','part':'edible_portion',
                     'evidence':'native_reader' if basis_supported else 'legacy_contract_assumption'},
            'values_per_100g':values,'known_nutrients':[m for m in METRICS if values[m] is not None],
            'core_complete':all(values[m] is not None for m in CORE),
            'nutrient_definitions':{'carbs_g':
                'Native EFSA component carbohydrate; definition not assumed to be carbohydrate by difference.'
                if profile and profile.get('native_values') else 'Legacy table field; analytical definition not independently validated.'},
            'unknown_properties':[k for k,v in (native['facets'] if native else annotation['facets']).items()
                                  if v in (None,'unknown')],
            'blockers':sorted(set(blockers)),'warnings':sorted(set(warnings)),
            'native_nutrient_evidence':profile,
            'policy_version':VERSION}
        self.cache[key]=result
        return copy.deepcopy(result)

    def check(self, collection: str, candidate: dict, query: dict) -> dict:
        facts=self.source(collection,candidate)
        rules=[{'rule_id':'source.'+b,'relation':'reject'} for b in facts['blockers']]
        native=facts['native_annotation']
        if native:
            interpreted=copy.deepcopy(native['text_annotation'])
            interpreted['facets']=copy.deepcopy(native['facets'])
            assessed=assess_compatibility(query,interpreted)
            rules.extend(r for r in assessed.rules if r['relation']=='reject')
        return {'blocking_rules':rules,'source_facts':facts}


def calculation_decision(observed: dict, candidate: dict | None, facts: dict | None, *,
                         selected: bool, alias_identity: bool=False, retained: bool=False,
                         selected_input: dict | None=None) -> dict:
    """Keep observed facts, source facts and assumptions independent on every route."""
    result={'kind':'unresolved','policy_version':VERSION,'observed_facts':copy.deepcopy(observed),
        'selected_record_facts':copy.deepcopy(facts),'assumptions':[], 'blockers':[],
        'values_per_100g':dict.fromkeys(METRICS),'missing_nutrients':list(METRICS),
        'validation':'Provisional in-house policy, not independently validated nutrition.'}
    recovery = observed.get('occurrence_recovery', {})
    if recovery.get('conflicts'):
        result['blockers'] = ['input.conflicting_occurrence_evidence']
        return result
    if not selected or candidate is None or facts is None:
        result['blockers']=['no_supported_selection'];return result
    if facts['blockers']:
        result['blockers']=facts['blockers'];return result
    from recipe_wrangler.matching.source_suitability import material_scope_conflicts
    scope_conflicts=material_scope_conflicts(selected_input or observed,facts['text_annotation'])
    if scope_conflicts:
        result['blockers']=scope_conflicts;return result
    original=observed
    observed=selected_input or observed
    annotation=facts['text_annotation']
    if facts.get('native_annotation'):
        annotation=copy.deepcopy(annotation)
        annotation['facets']=facts['native_annotation']['facets']
    annotation=copy.deepcopy(annotation)
    assumptions=result['assumptions']
    def assume(policy,field,value,reason):
        assumptions.append({'policy':policy,'field':field,'value':value,'reason':reason})
    for claim in recovery.get('recovered', []):
        if any(e['status'] != 'stated' for e in claim['evidence']):
            assume('occurrence_container_interpretation', claim['field'], claim['value'],
                   'Container wording interpreted as canned; processing remains a provisional inference.')
    if original.get('input_text')!=observed.get('input_text'):
        assume('explicit_alternative_selection','selected_alternative',observed.get('input_text'),
               'Uses the first-listed alternative and its own qualifiers; original alternatives remain observed facts.')
        if observed.get('inherited_qualifiers'):
            assume('alternative_qualifier_scope','constrained_fields',observed['inherited_qualifiers'],
                   'Conservatively retain explicit nutrition qualifiers across the selected alternative; qualifier scope remains provisional.')
    q,c=observed['facets'],annotation['facets']
    # Declared in-house class mapping, not a regulatory claim or calibrated threshold.
    if q.get('fat_level') in {'low','reduced'} and c.get('fat_level') is None and annotation.get('foods') and annotation['foods'][0].get('family')=='milk':
        match=re.fullmatch(r'(1(?:\.0)?|1\.5|1\.8|2(?:\.0)?)% fat',c.get('fat_content_description') or '')
        if match:
            c['fat_level']='low'
            assume('milk_numeric_fat_class','fat_level','low',
                'In-house mapping of explicitly labelled 1%, 1.5%, 1.8% or 2% milk; never inferred from profile fat grams and never maps 0% to low fat.')
    # Identical bare names cannot cover properties recovered outside those names.
    complete_description = not recovery.get('recovered')
    assessed=assess_compatibility(observed,annotation,
        exact_description=complete_description and exact_description_match(observed.get('input_text',''),facts['label']),
        catalogue_name=complete_description and catalogue_name_match(observed,annotation,facts['label']))
    contradictions=[r['rule_id'] for r in assessed.rules if r['relation']=='reject']
    # Curated identity aliases can explain the identity difference, not properties.
    if alias_identity:
        contradictions=[r for r in contradictions if not r.startswith('identity.')]
    if contradictions:
        result['blockers']=contradictions;return result
    identity=compare_identities(observed.get('main_ingredient'),annotation.get('main_ingredient'))
    supported=(alias_identity or identity.relation in {'exact','synonym','likely_equivalent'}
        or any(r['rule_id']=='identity.same_exact_dictionary_concept' for r in assessed.rules)
        or catalogue_name_match(observed,annotation,facts['label']))
    if facts['basis']['evidence']=='legacy_contract_assumption':
        assume('legacy_reporting_basis','nutrient_basis','per_100g_edible','Retained table contract; not newly measured or independently validated.')
    if not supported:
        assume('retained_identity_proxy','identity',facts['label'],'Existing selector evidence; complete semantic equivalence has not been established.')
    if q.get('preparation_state') is None and c.get('preparation_state')=='raw':
        assume('raw_input_default','preparation_state','raw','Unspecified ingredient state uses the explicitly raw source record for this estimate.')
    if c.get('preparation_state') is None and not c.get('preservation_methods'):
        assume('as_described_source_state','preparation_state','unknown',
               'Source cooking/input state is unspecified; use only as a provisional as-described profile, not as evidence of raw/cooked equivalence.')
    if q.get('preservation_methods')==[] and 'dried' in c.get('preservation_methods',[]):
        assume('dry_product_default','preservation_methods',['dried'],'Selected dry input basis; saved weights must refer to the dry ingredient.')
    if observed.get('foods') and observed['foods'][0].get('family')=='pasta':
        requested=observed.get('qualifiers',{}).get('pasta_shape')
        actual=annotation.get('qualifiers',{}).get('pasta_shape')
        if requested and requested!=actual:
            assume('pasta_shape_proxy','pasta_shape',actual or 'generic_pasta','Nutrient proxy only; observed pasta shape is retained for adaptation.')
    for field in ['fat_content_description','fat_level','fortification','biological_source','food_part',
                  'packing_medium','drainage_status','grain_refinement','colour','variety']:
        if q.get(field) in (None,'unknown') and c.get(field) not in (None,'unknown'):
            assume('source_specificity_estimate',field,c[field],'Property is stated by the source record, not by the input ingredient.')
    permitted_defaults=set()
    if q.get('preparation_state') is None and c.get('preparation_state')=='raw':
        permitted_defaults.add('property.preparation_state')
    if q.get('preservation_methods')==[] and 'dried' in c.get('preservation_methods',[]):
        permitted_defaults.add('property.preservation_methods')
    if any(a['policy']=='milk_numeric_fat_class' for a in assumptions):
        permitted_defaults.add('property.fat_content_description')
    unresolved_material=[r['rule_id'] for r in assessed.rules if r['relation']=='needs_review'
        and r['rule_id'].startswith(('property.','qualifier.','product.')) and r['rule_id'] not in permitted_defaults]
    if unresolved_material:
        assume('retained_property_uncertainty','compatibility',unresolved_material,
               'Retained estimate with incomplete property support; does not establish facet equivalence.')
    if not facts['core_complete']:
        assume('partial_nutrient_profile','missing_core',[m for m in CORE if facts['values_per_100g'].get(m) is None],
               'Blank source nutrients stay unknown unless reviewed fills or the missing-mass tolerance cover them; the line lists them in blank_nutrients_as_zero.')
    result['values_per_100g']=copy.deepcopy(facts['values_per_100g'])
    result['missing_nutrients']=[m for m in METRICS if facts['values_per_100g'].get(m) is None]
    legacy_uncertainty=any(a['policy'] in {'legacy_reporting_basis','retained_identity_proxy','retained_property_uncertainty'} for a in assumptions)
    result['kind']='retained_legacy_estimate' if retained or legacy_uncertainty else 'automatic_estimate' if assumptions else 'direct_match'
    return result
