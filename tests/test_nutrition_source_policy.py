import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.matching.nutrition_policy import (
    SourceNutrientPolicy, calculation_decision, METRICS,
)
from scripts.composition.build_irish_inhouse_comparison import live_nutrients, EU, scale, aggregate


@pytest.fixture
def annotator():
    specs=[('milk','milk'),('egg','egg'),('pasta','pasta'),('ginger','generic')]
    terms=[{'term_id':name,'preferred_label':name,'definition':'','curation_status':'',
            'labels':[{'text':name,'type':'preferred'}]} for name,_ in specs]
    vocabulary=FacetVocabulary({'catalogue_version':'test','facets':[{'code':'process','terms':[
        {'code':'A07GL','label':'Boiling','status':'APPROVED','parent_codes':[]}]}]})
    return HybridAnnotator(ContextualParser(IngredientDictionary({'schema_version':1,'terms':terms}),
        {name:{family} for name,family in specs}),vocabulary)


def policy(annotator, label='Milk', metadata=None):
    source={'dataset':'Table','release':'2026','record_id':'1'}
    record={'catalogue_key':'key','source':source,'label':label,'metadata':metadata or {}}
    values=dict(zip(METRICS,[45,3,5,1.5,None,1,42,None]))
    profile={'source':source,'label':label,'values_per_100g':values,
             'native_values':{'energy_kcal':[{'value':45,'raw_unit':'Kilocalories/100 gram'}]}}
    row={'id':'catalogue:key','food_name':label,'nutrients':{
        EU[m]:{'value':v,'unit':'kcal' if m=='energy_kcal' else 'mg' if m=='sodium_mg' else 'g'}
        for m,v in values.items() if v is not None}}
    provider=Mock();provider.nutrient.return_value=row
    foodex=SimpleNamespace(version='test',sha256='sha',resolve=lambda expression:SimpleNamespace(issues=(),terms=()))
    evaluator=SourceNutrientPolicy(provider,annotator,{'key':record},{'key':profile},foodex,live_nutrients)
    candidate={'id':'catalogue:key','document':label,'metadata':{
        'eu_id':'catalogue:key','catalogue_key':'key','catalogue_source':source}}
    return evaluator,candidate,row


def test_exact_released_profile_keeps_missing_values_and_units(annotator):
    evaluator,candidate,_=policy(annotator)
    facts=evaluator.source(COLLECTIONS['eu'],candidate)
    assert facts['blockers']==[] and facts['core_complete']
    assert facts['values_per_100g']['sodium_mg']==42
    assert facts['values_per_100g']['sugar_g'] is None
    assert facts['basis']['evidence']=='native_reader'
    assert facts['source']['release']=='2026'
    facts['values_per_100g']['sodium_mg']=0
    assert evaluator.source(COLLECTIONS['eu'],candidate)['values_per_100g']['sodium_mg']==42


def test_matcher_gate_and_calculation_use_the_same_first_listed_food(annotator):
    from recipe_wrangler.tools import nutrition_match as nm
    from recipe_wrangler.matching.candidate_gate import CompatibilityGatedCatalogue
    evaluator,candidate,_=policy(annotator,'Milk')
    evaluator.catalogue.errors=[]
    evaluator.catalogue.query.return_value=[candidate,{'id':'other','document':'Ginger','metadata':{}}]
    gate=CompatibilityGatedCatalogue(evaluator.catalogue,annotator,source_policy=evaluator)
    gate.begin_input('milk or ginger')
    with nm.matching_backend(gate,{}):
        selected=nm.best_nutrition_match('milk or ginger','eu',identity_name='milk or ginger')
    assert selected['selected_alternative']=='milk'
    assert selected['match']['id']==candidate['id']
    original=copy.deepcopy(gate.input_facts())
    result=calculation_decision(original,selected['match'],evaluator.source(COLLECTIONS['eu'],selected['match']),
        selected=True,selected_input=gate.semantic_input())
    assert result['kind']!='unresolved',result['blockers']
    assert result['values_per_100g']['energy_kcal']==45
    choice=next(a for a in result['assumptions'] if a['policy']=='explicit_alternative_selection')
    assert choice['value']=='milk' and 'first-listed' in choice['reason']
    assert result['observed_facts']==original
    assert [a['text'] for a in original['alternatives']]==['milk','ginger']


def test_exact_bare_name_does_not_cover_missing_occurrence_property(annotator):
    from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
    evaluator,candidate,_=policy(annotator,'Milk')
    observed=recover_occurrence_facts('Milk','200ml low-fat milk',annotator.annotate)
    result=calculation_decision(observed,candidate,evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind'] != 'direct_match'
    uncertainty=next(a for a in result['assumptions'] if a['policy']=='retained_property_uncertainty')
    assert 'property.fat_level' in uncertainty['value']


def test_container_interpretation_remains_an_explicit_calculation_assumption(annotator):
    from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
    evaluator,candidate,_=policy(annotator,'Milk, canned')
    observed=recover_occurrence_facts('Milk','1 tin of milk',annotator.annotate)
    result=calculation_decision(observed,candidate,evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert any(a['policy']=='occurrence_container_interpretation' for a in result['assumptions'])


@pytest.mark.parametrize('change,reason',[
    ('release','source_release_mismatch'),('label','source_label_mismatch'),
    ('unit','native_nutrient_linkage_mismatch:sodium_mg'),('value','native_nutrient_linkage_mismatch:energy_kcal'),
    ('basis','unsupported_reporting_basis:per_100ml'),('missing','missing_nutrient_row'),
    ('row_id','nutrient_row_id_mismatch'),('row_release','nutrient_row_release_mismatch'),
    ('row_label','nutrient_row_label_mismatch'),
])
def test_corrupt_source_linkage_is_blocked(annotator,change,reason):
    evaluator,candidate,row=policy(annotator)
    if change=='release':candidate['metadata']['catalogue_source']=dict(candidate['metadata']['catalogue_source'],release='2025')
    if change=='label':candidate['document']='Different food'
    if change=='unit':row['nutrients']['Sodium, Na']['unit']='g'
    if change=='value':row['nutrients']['Energy, kcal']['value']=999
    if change=='basis':row['reporting_basis']='per_100ml'
    if change=='missing':evaluator.catalogue.nutrient.return_value=None
    if change=='row_id':row['id']='different'
    if change=='row_release':row['catalogue_source']=dict(candidate['metadata']['catalogue_source'],release='2025')
    if change=='row_label':row['food_name']='Another food'
    assert reason in evaluator.source(COLLECTIONS['eu'],candidate)['blockers']


def test_native_cooked_assertion_blocks_exact_raw_name(annotator):
    evaluator,candidate,_=policy(annotator,'Egg, raw',{'FoodEx2 code':'A031G#F28.A07GL'})
    check=evaluator.check(COLLECTIONS['eu'],candidate,annotator.annotate('Egg, raw'))
    assert check['blocking_rules']
    assert 'source_label_disagreement:preparation_state' in check['source_facts']['blockers']
    decision=calculation_decision(annotator.annotate('Egg, raw'),candidate,check['source_facts'],selected=True)
    assert decision['kind']=='unresolved'
    assert all(v is None for v in decision['values_per_100g'].values())


def test_unmapped_native_descriptor_is_uncertainty_not_a_conflict(annotator):
    evaluator,candidate,_=policy(annotator,'Milk',{'FoodEx2 code':'A0000#F02.A069M'})
    facts=evaluator.source(COLLECTIONS['eu'],candidate)
    assert not facts['blockers']
    assert 'unprojected_native_descriptor:F02.A069M' in facts['warnings']


def test_raw_default_is_assumption_and_does_not_modify_observed_facets(annotator):
    evaluator,candidate,_=policy(annotator,'Ginger, raw')
    observed=annotator.annotate('ginger');original=copy.deepcopy(observed)
    result=calculation_decision(observed,candidate,evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind']=='automatic_estimate'
    assert any(a['policy']=='raw_input_default' for a in result['assumptions'])
    assert result['observed_facts']['facets']['preparation_state'] is None
    assert observed==original


def test_explicit_cooking_change_cannot_reuse_incompatible_source(annotator):
    evaluator,candidate,_=policy(annotator,'Egg, boiled')
    result=calculation_decision(annotator.annotate('egg, fried'),candidate,
        evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind']=='unresolved'
    assert result['values_per_100g']['energy_kcal'] is None


def test_numeric_milk_class_is_explicit_policy_not_observed_fact(annotator):
    evaluator,candidate,_=policy(annotator,'Milk, 1% fat')
    result=calculation_decision(annotator.annotate('low-fat milk'),candidate,
        evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind']=='automatic_estimate'
    assert any(a['policy']=='milk_numeric_fat_class' for a in result['assumptions'])
    assert result['observed_facts']['facets']['fat_content_description'] is None
    evaluator,candidate,_=policy(annotator,'Milk, 0% fat')
    result=calculation_decision(annotator.annotate('low-fat milk'),candidate,
        evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind']=='unresolved'


def test_legacy_basis_and_identity_are_not_upgraded_to_verified(annotator):
    evaluator,candidate,row=policy(annotator)
    candidate['metadata'].pop('catalogue_key')
    facts=evaluator.source(COLLECTIONS['eu'],candidate)
    result=calculation_decision(annotator.annotate('milk'),candidate,facts,selected=True,retained=True)
    assert result['kind']=='retained_legacy_estimate'
    assert any(a['policy']=='legacy_reporting_basis' for a in result['assumptions'])


def test_missing_nutrient_and_quantity_scaling_stay_separate(annotator):
    evaluator,candidate,_=policy(annotator)
    result=calculation_decision(annotator.annotate('Milk'),candidate,
        evaluator.source(COLLECTIONS['eu'],candidate),selected=True)
    assert result['kind']=='automatic_estimate'
    assert any(a['policy']=='as_described_source_state' for a in result['assumptions'])
    assert result['values_per_100g']['sugar_g'] is None
    assert scale(result['values_per_100g'],200)['energy_kcal']==90
    totals=aggregate([scale(result['values_per_100g'],100)],2)
    assert totals['total']['sugar_g'] is None
    assert totals['per_serving']['energy_kcal']==22.5


def test_gate_applies_source_checks_to_alias_ids(annotator):
    from recipe_wrangler.matching.candidate_gate import CompatibilityGatedCatalogue
    evaluator,candidate,row=policy(annotator)
    row['reporting_basis']='per_100ml'
    evaluator.catalogue.errors=[];evaluator.catalogue.by_id.return_value=candidate
    gate=CompatibilityGatedCatalogue(evaluator.catalogue,annotator,source_policy=evaluator)
    gate.begin_input('Milk')
    assert gate.by_id(COLLECTIONS['eu'],candidate['id']) is None
    assert gate.audit[-1]['source_check']['blockers']==['unsupported_reporting_basis:per_100ml']


def test_missing_servings_preserve_input_totals_without_inventing_portions():
    totals=aggregate([dict.fromkeys(METRICS,10)],None)
    assert totals['total']['energy_kcal']==10
    assert totals['serving_count'] is None
    assert all(v is None for v in totals['per_serving'].values())
    assert all(v is None for v in totals['supported_partial_sum_per_serving'].values())


def test_text_annotation_is_cached_across_distinct_source_profiles(annotator):
    from unittest.mock import patch
    evaluator,candidate,row=policy(annotator)
    another=copy.deepcopy(candidate);another['id']='catalogue:other';another['metadata']['eu_id']='catalogue:other'
    another['metadata']['catalogue_key']='other'
    evaluator.records['other']=dict(evaluator.records['key'],catalogue_key='other')
    evaluator.profiles['other']=copy.deepcopy(evaluator.profiles['key'])
    with patch.object(annotator,'annotate',wraps=annotator.annotate) as capture:
        evaluator.source(COLLECTIONS['eu'],candidate)
        evaluator.source(COLLECTIONS['eu'],another)
    assert capture.call_count==1


@pytest.mark.parametrize('query,label,reason',[
    ('Milk','Milk in syrup','product.unsupported_packing_medium'),
    ('ginger, dried','Ginger, raw','product.dried_raw_conflict'),
    ('wholemeal pasta','Pasta, white, dried, raw','product.wholegrain_refined_conflict'),
])
def test_alias_or_retention_cannot_bypass_final_material_checks(annotator,query,label,reason):
    evaluator,candidate,_=policy(annotator,label)
    result=calculation_decision(annotator.annotate(query),candidate,
        evaluator.source(COLLECTIONS['eu'],candidate),selected=True,alias_identity=True,retained=True)
    assert reason in result['blockers']
    assert result['kind']=='unresolved'
    assert all(value is None for value in result['values_per_100g'].values())


def test_wholemeal_occurrence_evidence_survives_bare_pasta_name(annotator):
    from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
    observed=recover_occurrence_facts('pasta','pasta, wholemeal',annotator.annotate)
    assert observed['input_text']=='pasta'
    assert observed['facets']['grain_refinement']=='wholegrain'
    evidence=next(r for r in observed['occurrence_recovery']['recovered'] if r['field']=='grain_refinement')
    assert evidence['evidence'][0]['quote']=='wholemeal'
