"""Allergen edits must be recipe-owned, never edits to shared Ingredient facts."""
from unittest.mock import Mock
import pytest
from recipe_wrangler.repositories import neo4j_recipes as repo


@pytest.mark.parametrize('labels,expected', [([],[]),([' Milk ','milk','sesame'],['milk','sesame'])])
def test_allergen_patch_is_one_recipe_property_write(monkeypatch,labels,expected):
    execute=Mock(return_value=[{'rid':'recipe-a'}])
    monkeypatch.setattr(repo,'run_query',execute)
    shared=Mock(side_effect=AssertionError('Must not open shared ingredient edit session'))
    monkeypatch.setattr(repo,'driver',Mock(session=shared))
    assert repo.update_recipe_in_neo4j('recipe-a',allergens=labels)
    execute.assert_called_once()
    query,params=execute.call_args.args
    assert params['recipe_id']=='recipe-a'
    assert params['declared_allergens']==expected
    assert 'r.declared_allergens = $declared_allergens' in query
    assert 'HAS_ALLERGEN' not in query and 'DELETE' not in query
    shared.assert_not_called()


def test_recipe_without_keyword_can_keep_explicit_claim(monkeypatch):
    calls=[]
    monkeypatch.setattr(repo,'run_query',lambda query,params:(calls.append((query,params)) or [{'rid':'r'}]))
    assert repo.update_recipe_in_neo4j('r',allergens=['sesame'])
    assert calls[0][1]['declared_allergens']==['sesame']
    assert 'Ingredient' not in calls[0][0]


def test_missing_recipe_does_not_write_any_ingredient_state(monkeypatch):
    monkeypatch.setattr(repo,'run_query',lambda query,params:[])
    shared=Mock(side_effect=AssertionError('No ingredient writes'))
    monkeypatch.setattr(repo,'driver',Mock(session=shared))
    assert repo.update_recipe_in_neo4j('missing',allergens=['milk']) is False
    shared.assert_not_called()


def test_non_allergen_patch_leaves_recipe_declarations_untouched(monkeypatch):
    execute=Mock(return_value=[{'rid':'r'}]);monkeypatch.setattr(repo,'run_query',execute)
    assert repo.update_recipe_in_neo4j('r',title='New title')
    assert 'declared_allergens' not in execute.call_args.args[1]


def test_empty_recipe_claim_cannot_clear_detected_shared_conflict(monkeypatch):
    shared_ingredient={'allergens':['milk']}
    recipes={'a':{'declared_allergens':['sesame']},'b':{'declared_allergens':['egg']}}
    def execute(query,params):
        assert 'HAS_ALLERGEN' not in query and 'DELETE' not in query
        recipes[params['recipe_id']]['declared_allergens']=params['declared_allergens']
        return [{'rid':params['recipe_id']}]
    monkeypatch.setattr(repo,'run_query',execute)
    monkeypatch.setattr(repo,'driver',Mock(session=Mock(side_effect=AssertionError('Shared write'))))
    assert repo.update_recipe_in_neo4j('a',allergens=[])
    assert recipes['a']['declared_allergens']==[]
    assert recipes['b']['declared_allergens']==['egg']
    assert shared_ingredient['allergens']==['milk']


def test_update_http_returns_actual_conflicts_and_uncertainty(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from recipe_wrangler.api.routers import recipes
    calls=[]
    monkeypatch.setattr(recipes,'update_recipe_in_neo4j',lambda **kwargs:(calls.append(kwargs) or True))
    monkeypatch.setattr(recipes,'_run_query',lambda *args,**kwargs:[])
    monkeypatch.setattr(recipes,'fetch_recipe_allergens_by_ids',lambda ids:{ids[0]:['milk']})
    monkeypatch.setattr(recipes,'cache_delete',lambda *args:None)
    monkeypatch.setattr(recipes,'commit_recipe',lambda *args:None)
    app=FastAPI();app.include_router(recipes.router)
    with TestClient(app) as client:
        response=client.patch('/recipes/r',json={'allergens':[]})
    assert response.status_code==200,response.text
    result=response.json()
    assert calls[0]['allergens']==[]
    assert result['allergens']==['milk']
    assert result['allergen_uncertainty'] is True
    assert result['allergen_assessment']['detected_allergens']==['milk']


def test_graph_candidate_filter_uses_recipe_claims_and_aliases(monkeypatch):
    from recipe_wrangler.schemas.models import FoodChatRequest
    calls=[]
    monkeypatch.setattr(repo,'run_query',lambda query,params:(calls.append((query,params)) or []))
    assert repo.fetch_foodchat_candidates(FoodChatRequest(
        user_profile={'allergies':['dairy']},quotas={'dinner':1},randomize=False,
    ))=={'dinner':[]}
    assert len(calls)==1
    query,params=calls[0]
    assert 'coalesce(r.declared_allergens, [])' in query
    assert {'milk','dairy'}<=set(params['allergen_labels'])
    assert 'HAS_ALLERGEN' in query


def test_graph_candidate_output_keeps_warning_for_declared_claim(monkeypatch):
    from recipe_wrangler.schemas.models import FoodChatRequest
    from recipe_wrangler.repositories import postgres_nutrition
    monkeypatch.setattr(repo,'run_query',lambda *args:[{
        'recipe_id':'r','title':'Plain sauce','ingredients':'sauce',
        'directions':'Mix.','allergens':['sesame'],
    }])
    monkeypatch.setattr(postgres_nutrition,'get_recipe_nutrition_batch',lambda ids:{})
    result=repo.fetch_foodchat_candidates(FoodChatRequest(quotas={'dinner':1},randomize=False))
    assert result['dinner'][0]['allergen_uncertainty'] is True
    assert result['dinner'][0]['allergen_assessment']['detected_allergens']==['sesame']


def test_graph_parameter_filter_includes_recipe_claims():
    from recipe_wrangler.schemas.models import RecipeSearchFilters
    from recipe_wrangler.tools.param_search import build_param_search_cypher
    query,_,params=build_param_search_cypher(RecipeSearchFilters(exclude_allergens=['dairy']))
    assert 'NONE(label IN coalesce(r.declared_allergens, [])' in query
    assert {'dairy','milk'}<=set(params['exclude_allergens'])
