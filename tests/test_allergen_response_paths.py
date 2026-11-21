"""Exercise real response functions without importing the memory-heavy profiling pipeline.

Full-router import remains a separately reported integration/resource check.
"""
import ast
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from recipe_wrangler.schemas.models import RecipeCardNutrition, FoodChatRequest, FoodChatResponse
from recipe_wrangler.tools import es_recipe_search as search
from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields, recipe_consumer_fields

ROUTER = Path(__file__).resolve().parents[1] / 'src/recipe_wrangler/api/routers/recipes.py'


def response_function(name, **dependencies):
    tree = ast.parse(ROUTER.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {'Any': Any, 'RecipeCardNutrition': RecipeCardNutrition,
                 'recipe_allergen_fields': recipe_allergen_fields, 'recipe_consumer_fields':recipe_consumer_fields, **dependencies}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(ROUTER), 'exec'), namespace)
    return namespace[name]


def test_legacy_hit_and_final_card_do_not_trust_stale_false_warning():
    card = search._hit_to_card({'_source': {
        'recipe_id': 'old', 'allergens': [], 'allergen_uncertainty': False,
        'allergen_evidence': [{'allergen': 'milk', 'presence': 'contains'}],
    }}, 'eu')
    result = response_function('_es_card')(card)
    assert result['allergen_uncertainty'] is True
    assert result['allergen_assessment']['detected_allergens'] == ['milk']
    assert 'peanut' in result['allergen_assessment']['unresolved_allergens']


def test_batch_nutrition_card_keeps_warning_when_nutrition_is_missing():
    result = response_function('_build_card_nutrition')('r', {'allergen_evidence': []}, None, [], None).model_dump()
    assert result['allergen_uncertainty'] is True
    assert result['allergen_assessment']['cross_contact_assessed'] is False


def test_analysis_presence_is_not_a_recipe_free_from_claim():
    function = response_function('_analysis_allergen_fields', extra_line_evidence=lambda have, lines: [],
        detect_allergen_evidence_from_names=lambda names: [
        {'allergen': 'peanut', 'ingredient': 'peanut butter', 'presence': 'contains'},
    ])
    result = function(['peanut butter'])
    assert result['allergens'] == ['peanut']
    assert result['allergen_uncertainty'] is True
    assert 'milk' in result['allergen_assessment']['unresolved_allergens']


def test_analysis_response_keeps_possible_evidence_out_of_flat_presence_labels():
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
    result = response_function('_analysis_allergen_fields', extra_line_evidence=lambda have, lines: [],
        detect_allergen_evidence_from_names=detect_allergen_evidence_from_names)(['dried apricots'])
    assert result['allergens'] == []
    assert result['allergen_assessment']['possible_allergens'] == ['sulphites']


def test_query_keeps_unknowns_visible_and_excludes_positive_declarations(monkeypatch):
    monkeypatch.setattr(search, "_ingredients_is_nested", lambda index: True)
    body = search.build_es_query(search.RecipeSearchConstraints(exclude_allergens=['peanut']))
    clauses = body['query']['bool']['must_not']
    positive = next(c['nested'] for c in clauses if c.get('nested', {}).get('path') == 'allergen_evidence')
    assert {'terms': {'allergen_evidence.presence': ['contains', 'present']}} in positive['query']['bool']['filter'][1]['bool']['should']
    assert 'allergen_uncertainty' not in str(body['query'])


def test_foodchat_serialization_preserves_unknown_warning(monkeypatch):
    from recipe_wrangler.catalog import foodchat, entities
    hit = {'_source': {'recipe_id': 'r', 'title': 'Recipe', 'ingredients': [], 'instructions': []}}
    fake = SimpleNamespace(alias='offline', es=SimpleNamespace(search_body=lambda *args: {'hits': {'hits': [hit]}}))
    monkeypatch.setattr(entities, 'recipe_entity', lambda: fake)
    monkeypatch.setattr(foodchat, 'attach_nutrition', lambda results, profile: results)
    result = foodchat.fetch_candidates_es(FoodChatRequest(quotas={'dinner': 1}, randomize=False))
    serialized = FoodChatResponse(results=result).model_dump()['results']['dinner'][0]
    assert serialized['allergen_uncertainty'] is True
    assert serialized['allergen_assessment']['reason_codes'] == ['allergen_absence_not_assessed']
