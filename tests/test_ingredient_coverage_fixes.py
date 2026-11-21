"""Whole-food coverage repairs with genuine choices and lookalikes as controls."""
import re
from unittest.mock import Mock,patch

import pytest

from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence,PLANT_STAPLE_PATTERNS,GROUP_RULES,positive_keyword_regex
from recipe_wrangler.utils.dietary_ingredient_evidence import canonical_ingredient_name
from recipe_wrangler.utils import dietary_classification as dc,foodon_matching as fm


def state(text,allergen):
    return next((r['presence'] for r in detect_allergen_evidence_from_names([text]) if r['allergen']==allergen),None)


@pytest.mark.parametrize('text,allergen',[
    ('1 can crabmeat, drained','crustacean_shellfish'),('3 or 4 drops Worcestershire sauce','fish'),
    ('almonds (whole or slivered)','tree_nut'),('deli white or yellow American cheese','milk'),
    ('1 lb. deli white or yellow American cheese, sliced thin','milk'),
    ('-- Wild salmon filet or steaks: 6-10 oz. per person.','fish'),
    ('1 tablespoon nam pla','fish'),('Nuoc Mam','fish'),
    ('2 slices hot toast, buttered','milk'),
])
def test_named_allergen_survives_form_and_quantity_choices(text,allergen):
    assert state(text,allergen)=='contains'


@pytest.mark.parametrize('text,allergen',[
    ('butter or vegetable oil','milk'),('fish sauce or soy sauce','fish'),
    ('salmon or beef steak','fish'),('salmon filet or beef steaks','fish'),
    ('salmon filet or steaks of beef','fish'),('salmon filet or steaks with pork','fish'),
    ('almonds or slivered coconut','tree_nut'),('almonds whole or slivered coconut','tree_nut'),
    ('white bread or yellow American cheese','milk'),('almonds (optional)','tree_nut'),
    ('Worcestershire sauce (optional)','fish'),('may contain crabmeat','crustacean_shellfish'),
    ('nam pla or soy sauce','fish'),('crabmeat or tofu','crustacean_shellfish'),
    ('imitation crabmeat','crustacean_shellfish'),('imitation crab meat','crustacean_shellfish'),
    ('artificial crab sticks','crustacean_shellfish'),
])
def test_genuine_choices_and_warnings_remain_possible(text,allergen):
    assert state(text,allergen)=='possible'


@pytest.mark.parametrize('text,allergen',[
    ('vegan crabmeat','crustacean_shellfish'),('vegan nam pla','fish'),
    ('vegan imitation crabmeat','crustacean_shellfish'),
    ('without imitation crabmeat','crustacean_shellfish'),('no artificial crab','crustacean_shellfish'),
    ('imitation crabmeat-free salad','crustacean_shellfish'),
    ('vegetarian Nuoc Mam','fish'),('fish-free nam pla','fish'),
    ('without crabmeat','crustacean_shellfish'),('without nam pla','fish'),
    ('vegan buttered toast','milk'),('toast buttered with vegan butter','milk'),
])
def test_alternative_products_and_exclusions_do_not_gain_animal_allergens(text,allergen):
    assert state(text,allergen) is None


@pytest.mark.parametrize('text,allergen',[
    ('imitation crabmeat with real crabmeat','crustacean_shellfish'),
    ('vegan nam pla with anchovies','fish'),('not vegan nam pla','fish'),
    ('not imitation crabmeat','crustacean_shellfish'),
    ('vegan buttered toast with milk','milk'),('not vegan buttered toast','milk'),
])
def test_claim_scoping_keeps_independent_animal_ingredients(text,allergen):
    assert state(text,allergen)=='contains'


@pytest.mark.parametrize('group',['vegan','vegetarian'])
@pytest.mark.parametrize('text',['sweetened coconut','hot toast','vegan butter','Earth Balance vegan butter','vanilla-flavored soymilk'])
def test_clear_whole_food_names_have_positive_rules_without_graph_links(text,group):
    patterns=PLANT_STAPLE_PATTERNS+[positive_keyword_regex(k) for k in GROUP_RULES[group]['positive_keywords']]
    assert any(re.fullmatch(p,canonical_ingredient_name(text)) for p in patterns)
    assert not any(re.fullmatch(p,canonical_ingredient_name(text+' with mystery sauce')) for p in patterns)


@pytest.mark.parametrize('text',['dairy-free butter','margarine','not vegan butter','vegan butter with honey',
    'sweetened coconut cream dessert','garlic toast','cheese toast','mayonnaise','soft tortillas','orange liqueur'])
def test_uncertain_or_compound_names_cannot_borrow_clear_food_rules(text):
    assert not any(re.fullmatch(p,canonical_ingredient_name(text)) for p in PLANT_STAPLE_PATTERNS)


@pytest.mark.parametrize('text',['crabmeat','nam pla','Nuoc Mam'])
@pytest.mark.parametrize('group',['vegan','vegetarian'])
def test_named_seafood_blocks_diets_without_graph_links(text,group):
    hits,presence=dietary_keyword_evidence(text,group)
    assert hits and presence=='contains'


@pytest.mark.parametrize('text',['vegan nam pla','vegetarian Nuoc Mam'])
def test_explicit_fish_sauce_alternatives_do_not_block_vegetarian(text):
    assert not dietary_keyword_evidence(text,'vegetarian')[0]


@pytest.mark.parametrize('name,identifier',[
    ('crabmeat','FOODON_03301081'),('crab meat','FOODON_03301081'),
    ('nam pla','FOODON_03317197'),('nuoc mam','FOODON_03317197'),
    ('soy milk','FOODON_03305289'),('soymilk','FOODON_03305289'),('hot toast','FOODON_03306255'),
])
def test_reviewed_synonyms_link_without_vectors(name,identifier,monkeypatch):
    monkeypatch.setattr(fm,'match_embedding',lambda *a,**k:pytest.fail('No embedding needed'))
    assert fm.match_ingredient_to_foodon(name,{})==(identifier,'reviewed_exact_alias',1.0,False)
    assert fm.supported_existing_link(name,identifier,'reviewed concept')


@pytest.mark.parametrize('name,identifier',[
    ('sweetened coconut','FOODON_03316908'),('vanilla-flavored soymilk','FOODON_03305289'),
])
def test_qualified_food_links_preserve_approximate_family_scope(name,identifier,monkeypatch):
    monkeypatch.setattr(fm,'match_embedding',lambda *a,**k:pytest.fail('No embedding needed'))
    assert fm.match_ingredient_to_foodon(name,{})==(identifier,'reviewed_food_family_alias',0.95,True)
    assert fm.match_reviewed_alias(name+' with milk') is None


def test_graph_positive_candidates_use_clear_wording_without_overriding_blockers():
    batch=[{'element_id':str(i),'name':name,'class_ids':[]} for i,name in enumerate(['sweetened coconut','vegan butter','hot toast'])]
    with patch.object(dc,'ingredient_link_batches',return_value=iter([batch])),patch.object(dc,'_scalar',return_value=3) as scalar:
        dc._classify_positive(Mock(),'vegan')
    query=scalar.call_args.args[1]
    assert 'rel.status <> "not_suitable"' in query and 'size(possible_allergens) = 0' in query
