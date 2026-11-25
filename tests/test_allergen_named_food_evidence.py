"""Named-food aliases retain free-from, choice and lookalike scope."""
import pytest

from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names


def states(name):
    return {e['allergen']: e['presence'] for e in detect_allergen_evidence_from_names([name])}


@pytest.mark.parametrize('name', ['baguette', 'breadsticks', 'bread sticks', 'crescent rolls', 'pizza crust', 'kaiser rolls', 'sub rolls', 'waffles', 'angel food cake'])
def test_named_wheat_products_supply_both_wheat_and_gluten_evidence(name):
    found = states(name)
    assert found['wheat'] == found['gluten'] == 'contains'


@pytest.mark.parametrize('name', ['gluten-free baguette', 'gluten free pizza crust', 'gluten-free waffles', 'gluten-free breadsticks'])
def test_named_wheat_products_respect_explicit_gluten_free_variant(name):
    found = states(name)
    assert found.get('gluten') != 'contains'
    assert found.get('wheat') != 'contains'


def test_optional_or_alternative_baguette_is_possible():
    assert states('baguette (optional)')['gluten'] == 'possible'
    assert states('baguette or rice cakes')['gluten'] == 'possible'


@pytest.mark.parametrize('name', ['soymilk', 'vanilla-flavored soymilk', 'unsweetened soymilk'])
def test_closed_spelling_soymilk_has_soy_and_does_not_imply_dairy(name):
    found = states(name)
    assert found['soy'] == 'contains'
    assert found.get('milk') != 'contains'


@pytest.mark.parametrize('name', ['champagne', 'dry vermouth', 'sweet white vermouth'])
def test_named_grape_wine_styles_follow_existing_wine_sulphite_policy(name):
    assert states(name)['sulphites'] == 'contains'


@pytest.mark.parametrize('name', ['champagne vinegar', 'vermouth vinegar', 'champagne with no added sulphites', 'wine with no added sulfites', 'wine without added sulphites'])
def test_wine_style_variants_keep_the_existing_possible_policy(name):
    assert states(name).get('sulphites') != 'contains'


@pytest.mark.parametrize('name', ['champagne grapes', 'champagne mango', 'champagne mangoes'])
def test_champagne_fruit_names_do_not_imply_wine(name):
    assert 'sulphites' not in states(name)


@pytest.mark.parametrize('name', ['rice cakes', 'cornflour', 'buckwheat', 'waffle iron', 'waffle-cut potatoes'])
def test_lookalikes_are_not_new_wheat_ingredients(name):
    assert states(name).get('gluten') != 'contains'
