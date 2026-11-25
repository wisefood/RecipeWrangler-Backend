import pytest

from recipe_wrangler.utils.seasoning_quantities import seasoning_quantity_assumption
from recipe_wrangler.matching.quantities import resolve_quantity


@pytest.mark.parametrize('name,text', [('salt', 'to taste'), ('black pepper', ''),
    ('sea salt', 'salt and pepper to taste'), ('pepper', 'pepper')])
def test_unquantified_seasoning_is_one_gram_per_recipe(name, text):
    assert seasoning_quantity_assumption(name, text)['weight_g'] == 1
    result = resolve_quantity(name, 0.5, text, serves=4)
    assert result['weight_g'] == 1
    assert result['quantity_evidence'] is None
    assert result['quantity_assumption']['status'] == 'estimated'


@pytest.mark.parametrize('name,text', [('salt', '2g salt'), ('salt', '1 tsp'),
    ('salt', 'a pinch of salt'), ('pepper', '1 pepper'), ('red pepper', 'to taste'),
    ('salted butter', 'to taste'), ('pepper sauce', ''), ('salt', 'one teaspoon salt')])
def test_explicit_quantities_and_other_foods_do_not_get_seasoning_default(name, text):
    assert seasoning_quantity_assumption(name, text) is None


def test_explicit_exclusion_stays_excluded():
    assert resolve_quantity('salt', 0, 'salt to taste')['weight_g'] == 0


def test_weight_tool_uses_same_policy_without_search_or_model(monkeypatch):
    from recipe_wrangler.tools import ingredient_weight_tool as tool
    monkeypatch.setattr(tool, 'canonical_name_to_usda', lambda *a, **k: pytest.fail('No search needed'))
    result = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['salt', 'black pepper'],
        'measurements': ['to taste', ''], 'return_details': True, 'debug': True})
    assert result['weights'] == [1, 1]
    assert all(d['quantity_assumption']['status'] == 'estimated' for d in result['details'])
