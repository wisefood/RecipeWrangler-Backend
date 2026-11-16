"""Pescatarian composition is assessed, never inferred from absent meat."""
from types import SimpleNamespace
from unittest.mock import Mock
import importlib.util
from pathlib import Path

import pytest

from recipe_wrangler.catalog import foodchat
from recipe_wrangler.repositories.neo4j_recipes import _classify_recipe_suitability
from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.tools import es_recipe_search as search
from recipe_wrangler.tools.param_search import build_param_search_cypher
from recipe_wrangler.utils.consumer_suitability import PESCATARIAN_CLASSIFICATION_VERSION, classification_version
from recipe_wrangler.utils.dietary_classification import _group_params
from recipe_wrangler.utils.es_recipe_evidence import assessed_diet_tags, normalize_consumer_suitability

_spec = importlib.util.spec_from_file_location('pesc_query_subset', Path(__file__).with_name('allergen_query_subset.py'))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.mark.parametrize('diet', ['pescatarian', 'pescatarian_safe'])
def test_search_paths_require_one_current_positive_assessment(diet):
    request = SimpleNamespace(constraints=SimpleNamespace(), user_profile=SimpleNamespace(diet=[diet]))
    paths = [search.build_es_query(search.RecipeSearchConstraints(diet_tags=[diet]))['query']['bool']['filter'],
             foodchat._hard_filters(request, [])]
    base = {'has_profile': True, 'tags': [diet], 'diet_tags': [diet], 'suitable_for': ['pescatarian']}
    positive = {'group': 'pescatarian', 'status': 'suitable', 'classification_version': PESCATARIAN_CLASSIFICATION_VERSION}
    for filters in paths:
        def accepted(rows):
            return all(_module.matches(clause, {**base, 'consumer_suitability': rows}) for clause in filters)
        assert not accepted([])
        assert accepted([positive])
        assert not accepted([{**positive, 'status': 'unknown'}])
        assert not accepted([{**positive, 'classification_version': 'old'}])
        assert not accepted([positive, {**positive, 'status': 'not_suitable'}])
        assert not accepted([positive, {**positive, 'status': 'malformed'}])
        assert accepted([positive, {**positive, 'status': 'not_suitable', 'classification_version': 'old'}])
        # Nested fields must agree on the same assessment, not across groups.
        assert not accepted([{**positive, 'status': 'unknown'}, {**positive, 'group': 'vegetarian'}])
    query, _, params = build_param_search_cypher(RecipeSearchFilters(diet_tags=[diet]))
    assert 'pescatarian_version' in params and 'diet_tags' not in params
    assert "name:'pescatarian'" in query and "s.status='suitable'" in query


@pytest.mark.parametrize('ingredients,expected', [
    (['salmon', 'milk', 'egg'], 'unknown'), ([], 'unknown'),
    (['unresolved sauce'], 'unknown'), (['chicken'], 'not_suitable'),
    (['fish and beef'], 'not_suitable'), (['vegan bacon with pork'], 'not_suitable'),
    (['gelatine'], 'unknown'), (['collagen'], 'unknown'),
    (['bovine gelatin'], 'not_suitable'), (['fish gelatin'], 'unknown'),
    (['kidney beans'], 'unknown'),
    (['beef tomato'], 'unknown'), (['vegan sausage'], 'unknown'),
])
def test_creation_blocks_meat_and_never_promotes_missing_ingredient_evidence(ingredients, expected):
    assert _classify_recipe_suitability('Plate', ingredients, {'fish', 'milk', 'egg'}, 'pescatarian')[0] == expected


def test_group_policy_is_versioned_and_presence_alone_does_not_resolve_a_mixture():
    params = _group_params('pescatarian')
    assert params['version'] == PESCATARIAN_CLASSIFICATION_VERSION
    assert classification_version('vegetarian') == 'vegan-vegetarian-v10'
    assert params['blocking_allergens'] == params['positive_allergens'] == []
    assert 'FOODON_00001006' in params['blocking_roots']
    assert 'FOODON_00001248' in params['positive_roots']
    assert not set(params['positive_roots']) & set(params['blocking_roots'])


def test_projection_preserves_source_claim_without_promoting_it_to_assessed_facet():
    unknown = normalize_consumer_suitability([], classification_version='vegan-vegetarian-v1')
    assert 'pescatarian_safe' not in assessed_diet_tags(['pescatarian_safe'], unknown)
    positive = normalize_consumer_suitability([
        {'group':'pescatarian', 'status':'suitable', 'classification_version':PESCATARIAN_CLASSIFICATION_VERSION},
    ], classification_version='vegan-vegetarian-v1')
    assert assessed_diet_tags([], positive) == ['pescatarian_safe']
    stale = normalize_consumer_suitability([
        {'group':'pescatarian', 'status':'suitable', 'classification_version':'legacy', 'reason_codes':['all_ingredients_suitable']},
    ], classification_version='vegan-vegetarian-v1')[-1]
    assert stale['status'] == 'unknown'
    assert stale['reason_codes'] == ['unsupported_classification_version']


def test_duplicate_assessment_order_cannot_hide_blocking_or_unresolved_evidence():
    from itertools import permutations
    rows = [{'group':'pescatarian', 'status':status, 'classification_version':PESCATARIAN_CLASSIFICATION_VERSION}
            for status in ['suitable', 'unknown', 'not_suitable']]
    for order in permutations(rows):
        assert normalize_consumer_suitability(list(order), classification_version='v')[-1]['status'] == 'not_suitable'


def test_current_assessment_supersedes_stale_derived_status_in_either_order():
    current={'group':'pescatarian','status':'suitable','classification_version':PESCATARIAN_CLASSIFICATION_VERSION}
    old={**current,'status':'not_suitable','classification_version':'old'}
    for rows in [[current,old],[old,current]]:
        assessment=normalize_consumer_suitability(rows,classification_version='vegan-vegetarian-v8')[-1]
        assert assessment['status']=='suitable' and assessment['classification_version']==PESCATARIAN_CLASSIFICATION_VERSION


def test_batch_materialization_requires_current_group_version_without_inventing_fato_class():
    from scripts.facets.tag_diet import _tag_suitable
    from scripts.neo4j.classify_vegan_vegetarian import _ensure_schema_and_origins
    session = Mock()
    session.run.return_value.single.return_value = {'n':1}
    assert _tag_suitable(session, 'pescatarian', apply=True) == 1
    params = session.run.call_args.kwargs
    assert params['tag'] == 'pescatarian_safe'
    assert params['version'] == PESCATARIAN_CLASSIFICATION_VERSION
    _ensure_schema_and_origins(session)
    groups = next(call.kwargs['groups'] for call in session.run.call_args_list if 'groups' in call.kwargs)
    assert next(group for group in groups if group['name'] == 'pescatarian')['fato_iri'] is None


def test_both_owner_queries_read_versioned_pescatarian_assessments():
    from recipe_wrangler.catalog import projection
    from scripts.catalog import build_recipes
    for query in [projection.RECIPE_QUERY, build_recipes.RECIPE_QUERY]:
        assert 'g.name = "pescatarian"' in query
        assert 's.classification_version = $pescatarian_version' in query
        assert '(r)-[:HAS_DECLARATION]->(recipe_declaration:AllergenDeclaration)' in query
        assert 'recipe_declaration.presence' in query
        assert '(r)-[:HAS_ALLERGEN]->(recipe_allergen:Allergen)' in query


def test_slim_cards_expose_assessment_and_do_not_promote_source_claims():
    from recipe_wrangler.api.routers.recipes import _build_card_nutrition
    source = {'tags':['pescatarian'], 'diet_tags':['pescatarian_safe']}
    unknown = _build_card_nutrition('r', source, None, [], None).model_dump()
    assert unknown['tags']==['pescatarian']
    assert unknown['consumer_suitability'][-1]['status']=='unknown'
    assert unknown['suitable_for']==[]
    source['consumer_suitability']=[{'group':'pescatarian','status':'suitable',
                                   'classification_version':PESCATARIAN_CLASSIFICATION_VERSION}]
    positive = _build_card_nutrition('r', source, None, ['fish'], None).model_dump()
    assert positive['suitable_for']==['pescatarian']
    assert positive['allergen_uncertainty'] is True


def test_stale_shortcuts_cannot_advertise_current_eligibility_in_any_card_path():
    from copy import deepcopy
    from recipe_wrangler.api.routers.recipes import _es_card,_build_card_nutrition,_catalog_recipe_payload
    from recipe_wrangler.api.routers.tools import _plan_card
    from recipe_wrangler.schemas.models import RecipeDetailResponse
    source={'recipe_id':'r','title':'Plate','duration':None,'serves':None,'tags':['vegan'],'suitable_for':['vegan','pescatarian'],
            'consumer_suitability':[{'group':'vegan','status':'suitable','classification_version':'vegan-vegetarian-v1'}]}
    cards=[_es_card(search._hit_to_card({'_source':source},'eu')), _plan_card(deepcopy(source)),
           _build_card_nutrition('r',source,None,[],None).model_dump(),
           RecipeDetailResponse(**_catalog_recipe_payload(source)).model_dump()]
    for card in cards:
        assert card['suitable_for']==[]
        assert all(item['status']=='unknown' for item in card['consumer_suitability'])
    assert source['tags']==['vegan'] and source['suitable_for']==['vegan','pescatarian']


def test_foodchat_response_preserves_current_assessments(monkeypatch):
    from recipe_wrangler.catalog import entities
    from recipe_wrangler.schemas.models import FoodChatRequest,FoodChatResponse
    source={'recipe_id':'r','title':'Fish plate','ingredients':[],'instructions':[],
            'allergens':['fish'],'consumer_suitability':[{'group':'pescatarian','status':'suitable',
                'classification_version':PESCATARIAN_CLASSIFICATION_VERSION}]}
    fake=SimpleNamespace(alias='offline',es=SimpleNamespace(search_body=lambda *args:{'hits':{'hits':[{'_source':source}]}}))
    monkeypatch.setattr(entities,'recipe_entity',lambda:fake)
    monkeypatch.setattr(foodchat,'attach_nutrition',lambda values,profile:values)
    result=foodchat.fetch_candidates_es(FoodChatRequest(quotas={'dinner':1},randomize=False))
    card=FoodChatResponse(results=result).model_dump()['results']['dinner'][0]
    assert card['suitable_for']==['pescatarian']
    assert card['consumer_suitability'][-1]['classification_version']==PESCATARIAN_CLASSIFICATION_VERSION
    assert card['allergen_uncertainty'] is True
    assert 'consumer_suitability' in foodchat._SOURCE_FIELDS
