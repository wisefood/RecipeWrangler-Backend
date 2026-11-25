"""HTTP through the real graph/calculator, with external inference/lookups controlled."""
import copy
import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def workflow(monkeypatch):
    from recipe_wrangler.api.routers import recipes
    from recipe_wrangler.matching import runtime
    from recipe_wrangler.tools.nutrition_completeness import METRICS
    parser=importlib.import_module('recipe_wrangler.tools.parse_recipe_tool')
    weight=importlib.import_module('recipe_wrangler.tools.ingredient_weight_tool')
    profiler=importlib.import_module('recipe_wrangler.tools.recipe_profiling_tool')
    case={}
    monkeypatch.setenv('NUTRITION_MATCHER_MODE','new')
    monkeypatch.setenv('NUTRITION_MATCHING_RELEASE','controlled-release')
    def parsed(payload):return copy.deepcopy(case['parsed'])
    monkeypatch.setattr(parser,'parse_recipe_tool',SimpleNamespace(invoke=parsed))
    monkeypatch.setattr(weight,'ingredient_weight_tool_usda',SimpleNamespace(
        invoke=lambda payload:{'weights':[5.0]*len(payload['ingredient_names']),'details':[]}))
    monkeypatch.setattr(profiler,'sustainability_tool_vector',SimpleNamespace(invoke=lambda payload:{'details':[]}))
    def no_prices(*args):raise FileNotFoundError('Controlled absent price catalogue')
    monkeypatch.setattr(profiler,'load_recipe_cost_calibration',no_prices)
    def no_write(*args,**kwargs):raise AssertionError('Unexpected persistence')
    monkeypatch.setattr(recipes,'_persist_profile_trace_best_effort',no_write)
    monkeypatch.setattr(runtime.store,'save_run',no_write)
    engine=object.__new__(runtime.MatchingEngine);engine.release_id='controlled-release'
    def resolve(name,region,original=''):
        known=bool(original)
        values={metric:1.0 for metric in METRICS};values.update(energy_kcal=100.,sugar_g=None)
        return {'decision':{'confidence':'strong' if known else 'none'},
            'calculation':{'kind':'retained_legacy_estimate' if known else 'unresolved',
                'values_per_100g':values if known else {},'assumptions':[],'blockers':[] if known else ['fixture_unknown']},
            'source_facts':{'label':name,'source_key':region,'native_id':'fixture-food'} if known else None,
            'observed_facts':{'facets':{}},'leading_alternatives':[]}
    engine.resolve=resolve
    monkeypatch.setattr(runtime,'engine',lambda release:engine)
    app=FastAPI();app.include_router(recipes.router)
    with TestClient(app) as client:yield client,case


def submit(workflow,request,*,names,lines,region,serves=2,originals=None):
    client,case=workflow
    case['parsed']={'title':'Controlled recipe','ingredient_names':names,'measurements':['']*len(names),
                    'directions':['Cook.'],'total_time':10,'serves':99}
    if originals is not None:
        case['parsed']['ingredient_original_texts'] = originals
    response=client.post('/recipes/profile',json={'raw_recipe':'\n'.join(lines),'region':region,
        'serves':serves,'persist_trace':False,'annotate':False})
    assert response.status_code==200,response.text
    body=response.json();request.node.user_properties.append(('workflow_response',body))
    return body


@pytest.mark.parametrize('region,source',[('EU','eu'),('IE','irish'),('HU','hungarian'),('SI','slovenian')])
@pytest.mark.parametrize('grams',[600,1300])
def test_http_preserves_source_mass_and_keeps_large_blank_nutrient_unknown(workflow,request,region,source,grams):
    name='Dried egg pasta, cooked, unsalted'
    body=submit(workflow,request,names=[name],lines=[f'{grams}g {name}'],region=region)
    detail=body['ingredients'][0]
    assert detail['original_weight_g']==5
    assert detail['weight_g']==grams
    assert detail['original_ingredient_text']==f'{grams}g {name}'
    assert detail['quantity_status']=='explicit_source_mass'
    assert body['serves']==2 and body['nutrition_source_key']==source
    assert body['profiling_totals'][f'total_energy_kcal_{source}']==grams
    assert body['profiling_totals'][f'total_energy_kcal_per_serving_{source}']==grams/2
    assert body['profiling_totals'][f'total_sugar_g_{source}'] is None
    assert 'sugar_g' in detail['blank_nutrients_as_zero']
    assert body['nutrition_completeness']['nutrient_status']['sugar_g']=='incomplete'
    assert body['nutrition_completeness']['missing_contributions']['sugar_g']==1
    assert body['nutrition_completeness']['missing_mass_share']['sugar_g']==1
    assert body['full_profile']['nutrition_summary']['sugar_g'] is None
    assert body['matching_metadata']['catalogue_release']=='controlled-release'
    assert body['matching_metadata']['quantity_input_hash']==detail['quantity_input_hash']
    assert 'matching_run_id' not in body['matching_metadata']
    assert 'trusted_serves' not in body


@pytest.mark.parametrize('region',['EU','IE','HU','SI'])
def test_http_shared_source_stays_unknown_with_visible_quantity_warning(workflow,request,region):
    body=submit(workflow,request,names=['milk','milk powder'],lines=['10g milk powder'],region=region)
    assert body['ingredient_original_texts']==['','']
    assert [d['source_context_status'] for d in body['ingredients']]==['missing_original_text']*2
    assert [d['weight_g'] for d in body['ingredients']]==[5,5]
    quality=body['profiling_quality']['nutrition_quality']
    assert quality['unverified_quantity_positions']==[0,1]
    assert body['nutrition_completeness']['nutrition_status']=='incomplete'
    assert body['nutrition_completeness']['complete_totals']['energy_kcal'] is None
    assert body['nutrition_completeness']['supported_partial_totals']['energy_kcal']==0
    assert 'milk' in body['allergens']


@pytest.mark.parametrize('extra',[{'serves':0},{'serves':-1},{'region':'XX'}])
def test_http_rejects_invalid_servings_or_region_before_pipeline(workflow,extra):
    client,case=workflow
    response=client.post('/recipes/profile',json={'raw_recipe':'100g pasta','persist_trace':False,
        'annotate':False,**extra})
    assert response.status_code==422
    assert case=={}


@pytest.mark.parametrize('region', ['EU', 'IE', 'HU', 'SI'])
def test_http_preserves_aligned_originals_with_cross_referencing_ingredients(workflow, request, region):
    lines = ['165g can of pineapple chunks in own juice',
             '65g pineapple juice (drained from the pineapple chunks)']
    body = submit(workflow, request, names=['pineapple chunks', 'pineapple juice'],
                  lines=lines, originals=lines, region=region)
    assert [d['original_ingredient_text'] for d in body['ingredients']] == lines
    assert all(d['source_context_status'] == 'matched_with_original_text' for d in body['ingredients'])


@pytest.mark.parametrize('region', ['EU', 'IE', 'HU', 'SI'])
def test_http_does_not_trust_duplicate_aligned_source_claims(workflow, request, region):
    body = submit(workflow, request, names=['milk', 'milk powder'], lines=['10g milk powder'],
                  originals=['10g milk powder', '10g milk powder'], region=region)
    assert [d['original_ingredient_text'] for d in body['ingredients']] == ['', '']
    assert body['profiling_quality']['nutrition_quality']['unverified_quantity_positions'] == [0, 1]


def test_http_preserves_distinct_aligned_occurrences_of_the_same_food(workflow, request):
    lines = ['1 teaspoon salt ½ teaspoon freshly ground nutmeg', 'pinch salt to taste']
    originals = [lines[0], '½ teaspoon nutmeg freshly ground', lines[1]]
    body = submit(workflow, request, names=['salt', 'nutmeg', 'salt'],
                  lines=lines, originals=originals, region='IE')
    assert [d['original_ingredient_text'] for d in body['ingredients']] == originals


def test_http_misaligned_originals_still_require_unique_source_binding(workflow, request):
    body = submit(workflow, request, names=['milk', 'milk powder'], lines=['10g milk powder'],
                  originals=['10g milk powder'], region='IE')
    assert [d['original_ingredient_text'] for d in body['ingredients']] == ['', '']
