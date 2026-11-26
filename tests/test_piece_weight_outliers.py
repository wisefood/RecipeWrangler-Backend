"""Piece weights behind Living Lab calorie outliers (R1-18 follow-up, 26 Nov).

Reference weights are USDA portions (usda-weights-v2): canned pineapple ring 47 g,
a cup of pineapple chunks 165 g, a tortilla chip 3 g, a corn tortilla 24 g, a pepper piece 10 g.
"""
from recipe_wrangler.tools import ingredient_weight_tool as mod


def grams(name, measurement):
    result = mod.ingredient_weight_tool_usda.invoke(
        {"ingredient_names": [name], "measurements": [measurement], "return_details": True, "debug": True}
    )
    return result["weights"][0]


def test_pineapple_parts_are_not_whole_pineapples():
    assert grams("pineapple rings", "6.0") == 6 * 47  # was 6 x 905 g
    assert grams("pineapple slices", "4.0") == 4 * 84  # USDA raw slice (3-1/2 inch)
    assert grams("pineapple chunks", "12.0") == 12 * 10
    assert grams("pineapple", "1.0") == 905  # a whole fruit stays whole


def test_wraps_and_tortillas_keep_their_own_weight():
    assert grams("spinach wraps", "4.0") == 4 * 50  # was a 250 g spinach bunch each
    assert grams("corn sweetcorn tortillas", "8.0") == 8 * 26  # was a 90 g corn cob each


def test_small_items_named_after_big_ones():
    assert grams("peppadew peppers", "5.0") == 5 * 10  # was a 120 g bell pepper each
    assert grams("corn chips", "3.5") == 3.5 * 3  # was 102 g each
    assert grams("petite red potatoes", "10.0") == 10 * 25  # was a 173 g potato each


def test_table_gaps_from_outlier_recipes():
    assert grams("dried apricots", "5.0") == 5 * 7  # was a 35 g fresh apricot each
    assert grams("shredded wheat", "1.0 cup") == 59  # USDA cup; the graph still holds 341 g from older code
    assert grams("store-bought coleslaw mix", "1 cup") == 70  # was 289 g
