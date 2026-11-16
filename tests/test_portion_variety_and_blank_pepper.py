"""Blank-amount spice pepper must not become a capsicum; variety words select the variety's portion."""
import pytest

from recipe_wrangler.tools import ingredient_weight_tool as tool
from recipe_wrangler.utils.seasoning_quantities import seasoning_quantity_assumption


def seasoning(name, measurement=''):
    """The explicit per-recipe salt/pepper policy where it applies, otherwise a pinch."""
    assumption = seasoning_quantity_assumption(name, measurement)
    return assumption['weight_g'] if assumption else tool.TO_TASTE_MIN_GRAMS


def grams(name, measurement, structured=False):
    result = tool.ingredient_weight_tool_usda.invoke({
        'ingredient_names': [name], 'measurements': [measurement], 'return_details': True, 'debug': False,
        'portion_inputs': [{'match_mode': 'ranked'}] if structured else None})
    return result['weights'][0]


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(tool, 'LIVE_WEIGHT_LLM_ENABLED', False)


@pytest.mark.parametrize('name', ['black pepper', 'freshly ground black pepper', 'ground black pepper',
                                  'white pepper', 'pepper', 'cracked black pepper', 'black peppercorns'])
@pytest.mark.parametrize('structured', [False, True])
def test_blank_spice_pepper_is_a_pinch(name, structured):
    assert grams(name, '', structured) == seasoning(name)


@pytest.mark.parametrize('structured', [False, True])
def test_bare_count_black_pepper_is_a_pinch(structured):
    assert grams('black pepper', '1', structured) == tool.TO_TASTE_MIN_GRAMS


@pytest.mark.parametrize('name,measurement', [('pepper', '1'), ('red pepper', ''), ('red pepper', '2'), ('bell pepper', '1')])
def test_capsicum_keeps_vegetable_weight(name, measurement):
    assert grams(name, measurement) > 50


def test_measured_pepper_is_untouched():
    assert grams('black pepper', '1 teaspoon') > tool.TO_TASTE_MIN_GRAMS


@pytest.mark.parametrize('measurement', ['optional', 'to taste'])
def test_structured_discretionary_amount_is_a_pinch(measurement):
    assert grams('black pepper', measurement, True) == seasoning('black pepper', measurement)


def test_structured_blank_salt_matches_legacy_pinch():
    assert grams('salt', '', True) == grams('salt', '') > 0


@pytest.mark.parametrize('name,count,each', [('cherry tomatoes', '6', 17.0), ('cherry tomatoes', '16', 17.0)])
def test_structured_cherry_tomatoes_use_cherry_portion(name, count, each):
    assert grams(name, count, True) == pytest.approx(float(count) * each)


def test_structured_plain_tomatoes_keep_whole_size():
    assert grams('tomatoes', '2', True) > 200


@pytest.mark.parametrize('name,original,expected', [
    ('pepper', '1 pepper, sliced', 'sweet pepper'),
    ('pepper', '1 medium pepper', 'sweet pepper'),
    ('Peppers', '2 peppers, thinly sliced', 'sweet pepper'),
    ('pepper', 'salt and pepper', 'pepper'),
    ('pepper', 'pepper, to taste', 'pepper'),
    ('pepper', '1/4 tsp pepper', 'pepper'),
    ('pepper', '', 'pepper'),
    ('black pepper', '1 black pepper', 'black pepper'),
])
def test_counted_bare_pepper_is_capsicum_for_matching(name, original, expected):
    from recipe_wrangler.matching.runtime import counted_pepper_name
    assert counted_pepper_name(name, original) == expected
