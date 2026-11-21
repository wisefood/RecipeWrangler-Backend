"""Internal regressions for exclusion scope, not independent accuracy labels."""
import pytest
from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergen_evidence_from_names,
    detect_allergens_from_names,
)


@pytest.mark.parametrize('ingredient,allergen', [
    ('coconut milk and cream', 'milk'),
    ('cream with coconut milk', 'milk'),
    ('soy milk; whey protein', 'milk'),
    ('vegan cheese plus milk powder', 'milk'),
    ('rice flour and wheat flour', 'wheat'),
    ('rice flour and wheat flour', 'gluten'),
    ('tamari; wheat flour', 'gluten'),
    ('buckwheat flour with rye flour', 'gluten'),
    ('rice pasta + ordinary pasta', 'wheat'),
])
def test_alternative_does_not_hide_another_clause(ingredient, allergen):
    assert allergen in detect_allergens_from_names([ingredient])
    evidence = detect_allergen_evidence_from_names([ingredient])
    assert any(v['allergen'] == allergen and v['ingredient'] == ingredient for v in evidence)


@pytest.mark.parametrize('ingredient,allergen', [
    ('coconut milk and almond cream', 'milk'),
    ('beans, butter', 'milk'),
    ('Milk, soya, non-dairy alternative to milk, unsweetened, fortified', 'milk'),
    ('apricot and coconut wheat & gluten-free muesli granola', 'wheat'),
    ('apricot and coconut wheat & gluten-free muesli granola', 'gluten'),
    ('gluten-free porridge oatmeal, cooked oats oatmeal uncooked', 'gluten'),
    ('cream, dairy-free', 'milk'),
    ('cheese, vegan', 'milk'),
    ('bread, gluten-free', 'wheat'),
    ('bread, gluten-free', 'gluten'),
    ('rice flour and potato flour', 'gluten'),
    ('rice flour and potato flour', 'wheat'),
    ('gluten baking flour', 'wheat'),
])
def test_alternative_only_names_keep_existing_exceptions(ingredient, allergen):
    assert allergen not in detect_allergens_from_names([ingredient])


@pytest.mark.parametrize("group", ["vegan", "vegetarian"])
@pytest.mark.parametrize("name", ["kidney beans", "red kidney beans", "canned white kidney beans"])
def test_plain_kidney_beans_do_not_trigger_organ_meat(group, name):
    from recipe_wrangler.repositories.neo4j_recipes import _classify_recipe_suitability
    status, blocking = _classify_recipe_suitability("Bean soup", [name], set(), group)
    assert (status, blocking) == ("unknown", [])


@pytest.mark.parametrize("name", ["kidney", "lamb kidneys", "kidney beans and beef"])
def test_kidney_bean_exception_does_not_hide_meat(name):
    from recipe_wrangler.repositories.neo4j_recipes import _classify_recipe_suitability
    assert _classify_recipe_suitability("Dinner", [name], set(), "vegetarian")[0] == "not_suitable"


def test_kidney_bean_exception_cannot_override_recipe_allergen_conflict():
    from recipe_wrangler.repositories.neo4j_recipes import _classify_recipe_suitability
    assert _classify_recipe_suitability("Dinner", ["kidney beans"], {"fish"}, "vegetarian")[0] == "not_suitable"
