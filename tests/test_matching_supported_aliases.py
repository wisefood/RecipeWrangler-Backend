from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.anatomy import explicit_anatomy, source_anatomy
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.matching.nutrition_policy import SourceNutrientPolicy, METRICS
from recipe_wrangler.matching.runtime import MatchingEngine


@pytest.mark.parametrize('name', ['chilli flakes', 'dried chili flakes', 'red chilli flakes', 'crushed red pepper flakes'])
def test_dried_chilli_flakes_have_an_explicit_reviewed_fallback(name):
    from recipe_wrangler.matching.reviewed_aliases import REVIEWED_SELECTIONS
    row = REVIEWED_SELECTIONS[name]
    assert row['eu_food_id'] == REVIEWED_SELECTIONS['crushed chillies']['eu_food_id']
    assert 'estimated' in row['approximate']
    assert 'fresh chilli' not in REVIEWED_SELECTIONS or REVIEWED_SELECTIONS['fresh chilli'] != row


@pytest.fixture
def annotator():
    names = ['chicken','turkey','duck','beef','pork','lamb','salmon','cod','milk','ginger','bagel',
             'stock','soup','pasta','egg','chicken stock','chicken soup']
    terms = [{'term_id': name, 'preferred_label': name, 'definition':'', 'curation_status':'',
              'labels': [{'text': name, 'type':'preferred'}]}
             for name in names]
    # The real failure occurs when an animal-product concept has no meat-family link.
    return HybridAnnotator(ContextualParser(IngredientDictionary({'schema_version':1,'terms':terms}),
        {'milk':{'milk'},'egg':{'egg'},'pasta':{'pasta'},'bagel':{'cereal'}}))


@pytest.mark.parametrize('label,source,part',[
    ('Chicken upper thigh without bone and skin raw','chicken','thigh'),
    ('Chicken, thigh (leg without drumstick), without skin, raw','chicken','thigh'),
    ('Chicken thigh raw w/o skin','chicken','thigh'),
    ('Turkey breast, raw','turkey','breast'),
    ('Duck, wing, raw','duck','wing'),
    ('Beef shoulder, raw','cattle','shoulder'),
    ('Pork, loin, raw','pig','loin'),
    ('Lamb shank, raw','lamb','shank'),
    ('Salmon fillet, raw','salmon','fillet'),
    ('Cod fillet, raw','cod','fillet'),
])
def test_literal_anatomy_does_not_depend_on_ontology_family(annotator,label,source,part):
    annotation=annotator.annotate(label)
    assert annotation['facets']['biological_source']==source
    assert annotation['facets']['food_part']==part
    assert not annotation['production_eligible']
    for key in ('biological_source','food_part'):
        assert any(e['field']=='facets.'+key and e['quote'] in label
                   for e in annotation['evidence'])
    assert explicit_anatomy(annotation)==annotation


@pytest.mark.parametrize('label',[
    'chicken stock','chicken soup','pasta with chicken thigh',
    'chicken with pork','chicken thigh or turkey breast',
    'chicken without thigh','chicken for breast',
    'chicken meat and skin, raw','chicken with breast sauce',
])
def test_compounds_alternatives_and_exclusions_do_not_gain_a_cut(annotator,label):
    annotation=annotator.annotate(label)
    assert annotation['facets']['food_part'] is None


def test_frozen_label_recovery_preserves_original_and_conflicts(annotator):
    annotation=annotator.annotate('Turkey thigh, raw')
    annotation['facets']['food_part']=None
    annotation['facets']['biological_source']=None
    annotation['evidence']=[e for e in annotation['evidence'] if e['field'] not in {
        'facets.food_part','facets.biological_source'}]
    before=deepcopy(annotation)
    repaired=explicit_anatomy(annotation)
    assert annotation==before
    assert repaired['facets']['food_part']=='thigh'
    assert repaired['facets']['biological_source']=='turkey'
    assert repaired['facets']['skin_status'] is None
    assert repaired['facets']['bone_status'] is None
    annotation['review_reasons'].append('conflicting_assertions:food_part')
    assert explicit_anatomy(annotation)['facets']['food_part'] is None
    annotation['facets']['food_part']='breast'
    assert explicit_anatomy(annotation)['facets']['food_part']=='breast'


def make_engine(annotator, name, labels, source_line=''):
    collection='nutritional_ingredients_eu'
    provider=Mock(errors=[])
    values={metric:1.0 for metric in METRICS}
    provider.nutrient.side_effect=lambda source,identifier:{'id':identifier,'food_name':labels[int(identifier)]}
    policy=SourceNutrientPolicy(provider,annotator,{}, {},None,lambda row:values)
    rows=[]
    for index,label in enumerate(labels):
        candidate={'id':str(index),'document':label,'metadata':{},'distance':.01,'rrf_score':.03}
        rows.append({'collection':collection,'candidate_id':str(index),'native_id':str(index),
            'candidate':candidate,'annotation':policy.source(collection,candidate),
            'nutrient_row':{'id':str(index),'food_name':label}})
    engine=MatchingEngine('test',rows=rows,manifest={'aliases':[
        {'alias':name,'eu_food_id':'0','food_name':labels[0]}]},annotator=annotator,
        foodex=SimpleNamespace())
    engine.search=Mock(side_effect=lambda selected,query,legacy=False:
        [deepcopy(row['candidate']) for row in rows] if selected==collection else [])
    return engine


@pytest.mark.parametrize('name,old,better',[
    ('turkey thigh','Turkey, meat, raw','Turkey thigh, raw'),
    ('duck breast','Duck, meat, raw','Duck breast, raw'),
    ('pork loin','Pork, meat, raw','Pork loin, raw'),
    ('lamb shank','Lamb, meat, raw','Lamb shank, raw'),
    ('salmon fillet','Salmon, raw','Salmon fillet, raw'),
    ('low-fat milk','Milk','Milk, low fat'),
    ('wholemeal bagel','Bagel','Bagel, wholemeal'),
    ('dried ginger','Ginger','Ginger, dried'),
    ('frozen ginger','Ginger','Ginger, frozen'),
    ('cooked pasta','Pasta','Pasta, cooked'),
])
def test_alias_with_missing_explicit_properties_examines_better_candidates(annotator,name,old,better):
    engine=make_engine(annotator,name,[old,better])
    result=engine.resolve(name,'eu',name)
    assert result['decision']['match']['id']=='1',result
    assert result['decision']['reason']=='positive_occurrence_support'
    assert engine.search.called
    assert any(c['candidate_id']=='1' for c in result['leading_alternatives'])


@pytest.mark.parametrize('bad',[
    'Turkey thigh, raw','Chicken breast, raw','Chicken thigh, cooked',
    'Chicken stock','Chicken thigh, raw, with skin',
])
def test_alias_probe_does_not_trade_uncertainty_for_a_conflict(annotator,bad):
    name='skinless chicken thigh'
    engine=make_engine(annotator,name,['Chicken high leg, meat, raw',bad])
    result=engine.resolve(name,'eu',name)
    assert result['decision'].get('match',{}).get('id')!='1',result


def test_fully_supported_alias_does_not_trigger_extra_search(annotator):
    engine=make_engine(annotator,'turkey thigh',['Turkey thigh, raw'])
    result=engine.resolve('turkey thigh','eu','turkey thigh')
    assert result['decision']['match']['id']=='0'
    engine.search.assert_not_called()


def test_missing_properties_keep_alias_as_labelled_estimate_when_no_better_exists(annotator):
    engine=make_engine(annotator,'turkey thigh',['Turkey, meat, raw'])
    result=engine.resolve('turkey thigh','eu','turkey thigh')
    assert result['decision']['match']['id']=='0'
    assert result['calculation']['kind']!='direct_match'
    assert any(a['policy']=='retained_property_uncertainty' for a in result['calculation']['assumptions'])
    assert engine.search.called


def test_cut_support_cannot_silently_change_an_existing_raw_basis(annotator):
    engine=make_engine(annotator,'turkey thigh',['Turkey, meat, raw','Turkey thigh, cooked'])
    result=engine.resolve('turkey thigh','eu','turkey thigh')
    assert result['decision']['match']['id']=='0'


def test_alias_replacement_requires_complete_core_nutrients(annotator):
    engine=make_engine(annotator,'turkey thigh',['Turkey, meat, raw','Turkey thigh, raw'])
    row=engine.ids['nutritional_ingredients_eu','1']
    row['annotation']['core_complete']=False
    row['annotation']['values_per_100g']['energy_kcal']=None
    result=engine.resolve('turkey thigh','eu','turkey thigh')
    assert result['decision']['match']['id']=='0'


def test_native_disagreement_cannot_be_filled_with_label_anatomy(annotator):
    text=annotator.annotate('Turkey thigh, raw')
    text['facets']['food_part']=None
    facts={'text_annotation':text,'native_annotation':{'facets':{'food_part':'breast'},
        'issues':['source_label_disagreement:food_part']}}
    recovered=source_anatomy(facts)
    assert recovered['native_annotation']['facets']['food_part']=='breast'
    assert recovered['native_annotation']['issues']==facts['native_annotation']['issues']


def test_alias_probe_queries_both_requested_region_and_global_catalogue(annotator):
    engine=make_engine(annotator,'turkey thigh',['Turkey, meat, raw','Turkey thigh, raw'])
    result=engine.resolve('turkey thigh','irish','turkey thigh')
    assert result['decision']['match']['id']=='1'
    assert {call.args[0] for call in engine.search.call_args_list}=={
        'nutritional_ingredients_eu','nutritional_ingredients_irish'}


def test_cut_support_cannot_drop_existing_skin_evidence(annotator):
    engine=make_engine(annotator,'turkey thigh',['Skinless turkey, raw','Turkey thigh, raw'])
    result=engine.resolve('turkey thigh','eu','turkey thigh')
    assert result['decision']['match']['id']=='0'


@pytest.mark.parametrize('label,part', [
    ('Chicken high leg, meat, raw', 'thigh'),
    ('Turkey upper leg, meat, raw', 'thigh'),
    ('Duck upper leg fillet, raw', 'thigh'),
    ('Chicken drumstick without bone and skin raw', 'drumstick'),
    ('Pork shoulder, raw', 'shoulder'),
    ('Beef sirloin, raw', 'sirloin'),
])
@pytest.mark.parametrize('typed', [False, True])
def test_anatomy_vocabulary_is_shared_across_typed_and_untyped_heads(annotator, label, part, typed):
    if typed:
        annotator.parser.families.update({name: {'meat'} for name in ('chicken','turkey','duck','pork','beef')})
    result = annotator.annotate(label)
    assert result['facets']['food_part'] == part, result
    assert not any('conflict' in reason and 'food_part' in reason for reason in result['review_reasons'])
    assert any(e['field']=='facets.food_part' and e['quote'] in label for e in result['evidence'])


def test_cached_high_leg_recovers_thigh_without_changing_skin_bone_or_nutrients(annotator):
    annotation = annotator.annotate('Chicken high leg, meat, raw')
    annotation['facets']['food_part'] = None
    annotation['evidence'] = [e for e in annotation['evidence'] if e['field'] != 'facets.food_part']
    annotation['modifiers'] = [m for m in annotation['modifiers'] if m['field'] != 'food_part']
    facts = {'text_annotation':annotation, 'values_per_100g':{'energy_kcal':116.}}
    before = deepcopy(facts)
    result = source_anatomy(facts)
    assert result['text_annotation']['facets']['food_part'] == 'thigh'
    assert result['text_annotation']['facets']['skin_status'] is None
    assert result['text_annotation']['facets']['bone_status'] is None
    assert result['values_per_100g'] == before['values_per_100g']
    assert facts == before


def test_supported_high_leg_alias_has_a_thigh_without_recipe_specific_mapping(annotator):
    engine = make_engine(annotator, 'chicken thigh', ['Chicken high leg, meat, raw'])
    result = engine.resolve('chicken thigh', 'eu', 'chicken thigh')
    assert result['decision']['match']['id'] == '0'
    assert engine.ids['nutritional_ingredients_eu', '0']['annotation']['text_annotation']['facets']['food_part'] == 'thigh'
    engine.search.assert_not_called()
