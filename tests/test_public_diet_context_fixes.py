"""Public-source regressions and counterexamples, not independent accuracy gold."""
import pytest

from recipe_wrangler.repositories.neo4j_recipes import (
    _allergen_keyword_evidence, _classify_recipe_suitability,
)
from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence


@pytest.mark.parametrize('text', [
    '420g can butter beans , drained', '400g can kidney beans in water, drained',
    '125ml unsweetened almond milk', '400g can coconut milk',
    '350ml rice milk (preferably unsweetened)', '100g coconut yogurt',
    '150g dairy-free soft cheese', '200g vegan plant butter block, softened',
    'vegan hard cheese', 'Vegan mac and cheese',
    'Chilli, maple & ginger glazed vegan cocktail sausages',
    '12 meat-free cocktail sausages', '300g oyster and shiitake mushroom',
    'Sunflower butter', 'Pistachio & cardamom butter',
    'cream of coconut', 'nut milk',
])
def test_plant_phrases_do_not_become_animal_evidence(text):
    for group in ['vegan', 'vegetarian']:
        hits, state = dietary_keyword_evidence(text, group)
        assert not hits or state != 'contains', (text, group, hits)
        assert _classify_recipe_suitability(text, [text], set(), group)[0] == 'unknown'


@pytest.mark.parametrize('text', [
    "100g soft goat's cheese, sliced", "Goat’s cheese & caramelised onion frittata",
    "Beetroot & mango salad with soft goat’s cheese",
    "Pan-fried asparagus with goat's curd",
])
def test_goat_cheese_is_dairy_rather_than_meat(text):
    assert not dietary_keyword_evidence(text, 'vegetarian')[0]
    assert dietary_keyword_evidence(text, 'vegan')[1] == 'contains'
    assert dietary_keyword_evidence(text, 'vegan')[0]
    assert _allergen_keyword_evidence(text, 'milk')[0]


@pytest.mark.parametrize('text', [
    'almond milk with milk', 'vegan cheese contains milk',
    'vegan mac and cheese with bacon', 'vegan cocktail sausages and pork',
    '400g kidney beans with beef', 'butter beans in butter',
    '300g oyster and shiitake mushrooms with oysters',
    'plant milk containing casein', 'vegan gravy containing milk',
    "goat's cheese with animal rennet", 'vegan chicken with egg',
    'not vegan cheese', 'non-vegan cheese', 'not dairy-free milk',
    'not meat-free sausages', 'plant milk with sunflower and butter',
])
def test_an_independent_animal_ingredient_still_blocks_vegan(text):
    hits, state = dietary_keyword_evidence(text, 'vegan')
    assert hits and state == 'contains'


@pytest.mark.parametrize('text,allergen', [
    ('200ml plant milk', 'milk'),
    ('cream of coconut', 'milk'), ('nut milk', 'milk'),
    ('vegan parmesan cheese', 'milk'),
    ('300g oyster and shiitake mushroom', 'molluscs'),
    ('1 tbsp coconut yogurt, to serve (optional)', 'milk'),
])
def test_allergen_exceptions_are_shared(text, allergen):
    assert not _allergen_keyword_evidence(text, allergen)[0]


@pytest.mark.parametrize('text,allergen', [
    ('plant milk containing casein', 'milk'),
    ('cream of coconut with cream', 'milk'), ('nut milk with milk', 'milk'),
    ('vegan gravy containing milk', 'milk'),
    ('not vegan cheese', 'milk'), ('non-vegan cheese', 'milk'),
    ('not dairy-free milk', 'milk'),
    ('almond milk mixed into cream', 'milk'),
    ('vegan cheese plus milk powder', 'milk'),
    ('oyster and shiitake mushrooms with oysters', 'molluscs'),
    ('oyster sauce and shiitake mushrooms', 'molluscs'),
    ('almond milk', 'tree_nut'), ('soy milk', 'soy'),
])
def test_other_allergen_evidence_is_not_erased(text, allergen):
    hits, state = _allergen_keyword_evidence(text, allergen)
    assert hits and state == 'contains'


def test_warning_and_optional_serving_are_not_confirmed_presence():
    assert _allergen_keyword_evidence('almond milk (may contain milk)', 'milk') == (['milk'], 'possible')
    assert _allergen_keyword_evidence('1 tbsp sour cream, to serve (optional)', 'milk')[1] == 'possible'
    assert dietary_keyword_evidence('1 tbsp sour cream, to serve (optional)', 'vegan')[1] == 'possible'
    assert _allergen_keyword_evidence('milk with optional almonds', 'milk')[1] == 'contains'


def test_vegetarian_claim_does_not_exempt_dairy_from_vegan_policy():
    assert dietary_keyword_evidence('vegetarian cheese', 'vegan')[0]
    assert _allergen_keyword_evidence('vegetarian cheese', 'milk')[0]


@pytest.mark.parametrize('title', ['Matcha mochi ice cream', 'Vegan chocolate banana ice cream'])
def test_generic_frozen_dessert_title_does_not_invent_dairy(title):
    assert _classify_recipe_suitability(title, ['banana'], set(), 'vegan')[0] == 'unknown'


@pytest.mark.parametrize('title,names', [
    ('Ice cream', ['ice cream']), ('Ice cream with milk', ['banana']),
    ('Ice cream with bacon', ['banana']), ('Ice cream', ['milk']),
])
def test_ice_cream_title_exception_cannot_hide_actual_animal_food(title, names):
    assert _classify_recipe_suitability(title, names, set(), 'vegan')[0] == 'not_suitable'


def test_positive_ontology_evidence_requires_identity_vetted_links(monkeypatch):
    from recipe_wrangler.utils import dietary_classification as dc
    from types import SimpleNamespace
    batch=[{'element_id':'1','name':'plain greek yogurt','class_ids':['dairy']},
           {'element_id':'2','name':'unknown mixture','class_ids':[]}]
    monkeypatch.setattr(dc,'ingredient_link_batches',lambda *a,**k:iter([batch]))
    calls=[]
    class Session:
        def run(self,query,**params):
            calls.append((str(query),params))
            return SimpleNamespace(single=lambda:{'count':1})
    assert dc._classify_positive(Session(),'vegetarian',['1','2'])==1
    assert calls[0][1]['candidates']==batch
    assert 'mapped.foodon_id IN item.class_ids' in calls[0][0]
    assert 'toLower(coalesce(mapped.label' not in calls[0][0]


def test_nut_milk_cannot_use_an_animal_dairy_family_alias():
    from recipe_wrangler.utils.foodon_matching import match_reviewed_alias,supported_existing_link
    assert match_reviewed_alias('nut milk') is None
    assert not supported_existing_link('nut milk','FOODON_00001256','dairy food product')


def test_family_presence_is_not_positive_proof_of_a_compound_food():
    from recipe_wrangler.utils.foodon_matching import ingredient_link_batches
    class Session:
        def run(self, query, params):
            if params['after']:return []
            return [{'element_id':'1','name':'milk mushroom stock',
                     'classes':[{'foodon_id':'FOODON_00001256','label':'dairy food product'}]}]
    assert next(ingredient_link_batches(Session()))[0]['class_ids']==['FOODON_00001256']
    assert next(ingredient_link_batches(Session(),positive_evidence=True))[0]['class_ids']==[]


def test_whole_dairy_names_support_vegetarian_but_not_compound_stock():
    import re
    from recipe_wrangler.utils.consumer_suitability import positive_keyword_regex
    assert re.fullmatch(positive_keyword_regex('milk'),'whole milk')
    assert re.fullmatch(positive_keyword_regex('yogurt'),'plain greek yogurt')
    assert not re.fullmatch(positive_keyword_regex('milk'),'milk mushroom stock')
