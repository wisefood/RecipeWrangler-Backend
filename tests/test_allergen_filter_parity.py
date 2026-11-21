"""Internal filter-contract regressions; ES execution is evaluated separately."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from recipe_wrangler.catalog.foodchat import allergen_exclusions
from recipe_wrangler.utils.es_recipe_evidence import known_allergen_exclusions, recipe_allergen_fields

_helper=Path(__file__).with_name('allergen_query_subset.py')
_spec=importlib.util.spec_from_file_location('allergen_query_subset',_helper)
_module=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_module)
matches=_module.matches


def excluded(clauses,doc):
    return any(matches(clause,doc) for clause in clauses)


@pytest.mark.parametrize('allergy,label',[
    ('dairy','milk'),('milk','dairy'),('eggs','egg'),('nuts','peanut'),
    ('nuts','tree_nut'),('tree nuts','tree_nut'),('shellfish','molluscs'),
    ('shellfish','crustacean_shellfish'),('soya','soy'),('sulfites','sulphites'),
])
def test_alias_requests_exclude_canonical_positive_labels(allergy,label):
    assert excluded(known_allergen_exclusions([allergy]),{'allergens':[label]})
    assert excluded(allergen_exclusions(allergy),{'allergens':[label]})


@pytest.mark.parametrize('presence,expected',[
    ('contains',True),('present',True),(None,True),('',True),('possible',False),
    ('may_contain',False),('unknown',False),('absent',False),
])
def test_positive_legacy_evidence_and_uncertain_statuses(presence,expected):
    evidence={'allergen':'milk','ingredient':'sauce'}
    if presence is not None:evidence['presence']=presence
    doc={'allergen_evidence':[evidence]}
    assert excluded(known_allergen_exclusions(['dairy']),doc) is expected


@pytest.mark.parametrize('allergy,name',[
    ('milk','coconut milk'),('dairy','soy milk'),('milk','butter beans'),
    ('eggs','eggplant'),('nuts','coconut'),('nuts','ground nutmeg'),
    ('tree nuts','peanut butter'),('shellfish','oyster mushrooms'),
])
def test_reviewed_name_exceptions_remain_unknown_and_visible(allergy,name):
    doc={'ingredients':[{'name':name}],'allergens':[],'allergen_evidence':[]}
    assert not excluded(allergen_exclusions(allergy),doc)
    assert recipe_allergen_fields([],[])['allergen_uncertainty'] is True


@pytest.mark.parametrize('allergy,label,name',[
    ('milk','milk','coconut milk'),('eggs','egg','eggplant'),
    ('shellfish','molluscs','oyster mushrooms'),('nuts','tree_nut','coconut'),
])
def test_positive_declaration_overrides_name_exception(allergy,label,name):
    doc={'ingredients':[{'name':name}],'allergens':[label]}
    assert excluded(allergen_exclusions(allergy),doc)


@pytest.mark.parametrize('allergy,name',[
    ('milk','coconut milk with cream'),('nuts','peanut butter'),
    ('tree nuts','almond butter'),('shellfish','oyster sauce'),
    ('milk','mozzarella'),('milk','buttermilk'),('eggs','eggplant with eggs'),
])
def test_other_lexical_conflicts_stay_excluded(allergy,name):
    assert excluded(allergen_exclusions(allergy),{'ingredients':[{'name':name}]})


def test_foodchat_relaxation_keeps_conflicts_out_and_serializes_unknown_warning(monkeypatch):
    from recipe_wrangler.catalog import foodchat,entities
    from recipe_wrangler.schemas.models import FoodChatRequest,FoodChatResponse
    calls=[]
    rows=[{'recipe_id':'known','title':'Conflict','ingredients':[{'name':'plain sauce'}],
           'allergens':['milk'],'allergen_evidence':[]},
          {'recipe_id':'unknown','title':'Coconut dish','ingredients':[{'name':'coconut milk'}],
           'allergens':[],'allergen_evidence':[]}]
    def search_body(index,body):
        calls.append(body)
        if len(calls)==1:return {'hits':{'hits':[]}}
        clauses=[q for f in body['query']['bool']['filter'] for q in f.get('bool',{}).get('must_not',[])]
        return {'hits':{'hits':[{'_source':r} for r in rows if not excluded(clauses,r)]}}
    fake=SimpleNamespace(alias='offline',es=SimpleNamespace(search_body=search_body))
    monkeypatch.setattr(entities,'recipe_entity',lambda:fake)
    monkeypatch.setattr(foodchat,'attach_nutrition',lambda rows,profile:rows)
    request=FoodChatRequest(quotas={'dinner':1},randomize=False,
                           user_profile={'allergies':['dairy']},constraints={'cuisines':['thai']})
    result=FoodChatResponse(results=foodchat.fetch_candidates_es(request)).model_dump()
    assert len(calls)>=2
    assert [v['recipe_id'] for v in result['results']['dinner']]==['unknown']
    assert result['results']['dinner'][0]['allergen_uncertainty'] is True
    for body in calls:
        assert 'allergen_evidence' in str(body['query'])



def test_v2_planner_card_rederives_warning_from_legacy_evidence():
    from recipe_wrangler.api.routers.tools import CARD_FIELDS, _plan_card
    assert "allergen_evidence" in CARD_FIELDS
    card = _plan_card({"recipe_id": "legacy", "ingredients": [{"name": "sauce"}],
        "allergens": [], "allergen_uncertainty": False,
        "allergen_evidence": [{"allergen": "milk", "presence": "possible"}]})
    assert card["allergen_uncertainty"] is True
    assert card["allergen_assessment"]["possible_allergens"] == ["milk"]


def test_v2_find_route_keeps_positive_alias_conflict_out_and_warning_visible(monkeypatch):
    from recipe_wrangler.api.routers import tools
    rows = [{"recipe_id": "positive", "title": "Known", "allergens": ["milk"], "ingredients": []},
            {"recipe_id": "uncertain", "title": "Coconut", "allergens": [],
             "allergen_evidence": [], "ingredients": [{"name": "coconut milk"}]}]
    def search(**kwargs):
        clauses = [q for f in kwargs["filters"] for q in f.get("bool", {}).get("must_not", [])]
        return {"results": [dict(row) for row in rows if not excluded(clauses, row)]}
    monkeypatch.setattr(tools, "recipe_entity", lambda: SimpleNamespace(search=search))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tools.router)
    from recipe_wrangler.api.identity import Caller
    app.dependency_overrides[tools.get_caller] = lambda: Caller()
    with TestClient(app) as client:
        response = client.post("/api/v2/tools/find_recipes", json={"exclude_allergens": ["dairy"]})
    assert response.status_code == 200
    result = response.json()
    assert [row["recipe_id"] for row in result["results"]] == ["uncertain"]
    assert result["results"][0]["allergen_uncertainty"] is True



def test_v1_search_http_keeps_unknown_with_warning_and_excludes_alias_conflicts(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from recipe_wrangler.api.routers import recipes
    from recipe_wrangler.tools import es_recipe_search as search
    docs = [{"recipe_id": "positive", "title": "Known", "allergens": ["milk"]},
            {"recipe_id": "possible", "title": "Unresolved", "allergens": [],
             "allergen_evidence": [{"allergen": "milk", "presence": "possible"}]}]
    monkeypatch.setattr(search, "_ingredients_is_nested", lambda index: True)
    def retrieve(constraints):
        body = search.build_es_query(constraints)
        clauses = body["query"]["bool"]["must_not"]
        # Other clauses concern disabled records; all fixtures here are active.
        allergen_clauses = [q for q in clauses if "terms" in q or q.get("nested", {}).get("path") == "allergen_evidence"]
        hits = [search._hit_to_card({"_source": doc}, "eu") for doc in docs if not excluded(allergen_clauses, doc)]
        return {"results": hits, "total": len(hits), "facets": {}}
    monkeypatch.setattr(recipes, "search_recipes_es", retrieve)
    app = FastAPI()
    app.include_router(recipes.router)
    with TestClient(app) as client:
        response = client.post("/recipes/search", json={"question": "", "exclude_allergens": ["dairy"]})
    assert response.status_code == 200
    cards = response.json()["results"]
    assert [card["recipe_id"] for card in cards] == ["possible"]
    assert cards[0]["allergen_uncertainty"] is True
    assert cards[0]["allergen_assessment"]["possible_allergens"] == ["milk"]
