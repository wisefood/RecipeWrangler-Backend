from unittest.mock import patch

from recipe_wrangler.tools import ingredient_weight_tool as mod


def grams(name, measurement):
    result = mod.ingredient_weight_tool_usda.invoke(
        {"ingredient_names": [name], "measurements": [measurement], "return_details": True, "debug": True}
    )
    return result["weights"][0]


def test_bare_head_lettuce_and_sprouts_use_usda_item_weights():
    assert grams("iceberg lettuce", "1.0") == 539
    assert grams("cos lettuce", "1.0") == 626
    assert grams("brussels sprouts", "4.0") == 76
    assert grams("english muffin", "2.0 whole") == 114
    assert grams("portobello mushrooms", "2.0") == 168


def test_turnip_size_uses_usda_size_weights_and_lettuce_heads():
    assert grams("turnip", "0.5 small") == 30.5
    assert grams("turnips", "4.0 small") == 244
    assert grams("lettuce", "1.0") == 500  # generic lettuce head reference
    assert grams("crispy lettuce", "1.0") == 539  # iceberg-type head
    assert grams("(600g) skinless chicken thigh fillets", "4 medium") == 600  # explicit mass wins


def test_reviewed_cup_densities_cover_common_dry_volumes():
    assert round(grams("golden caster sugar", "79.0 ml"), 1) == 65.8
    assert round(grams("rolled oats", "195.0 ml"), 1) == 65.8
    assert round(grams("plain flour", "1 dessertspoon"), 1) == 5.2
    assert round(grams("tomato paste", "2 teaspoons"), 1) == 10.9
    assert round(grams("baby spinach", "960.0 ml"), 1) == 120.0


def test_reviewed_cup_table_does_not_capture_lookalike_foods():
    assert round(grams("sugar snap peas", "1 cup"), 1) == 134.0  # peas, not sugar
    assert round(grams("cream cheese", "1 cup"), 1) != 112.0
    assert round(grams("asian-style salad dressing", "75 ml"), 1) == 75.0  # dressing density, not salad leaves


def test_unknown_solid_in_a_spoon_gets_a_flagged_convention():
    result = mod.ingredient_weight_tool_usda.invoke(
        {"ingredient_names": ["xanthan gum"], "measurements": ["1.0 tbsp"], "return_details": True, "debug": True}
    )
    assert result["weights"] == [12.0]
    assert result["details"][0]["match_type"] == "generic_spoon_convention"


def test_second_pass_items_and_bare_salt_oil_policy():
    assert grams("cucumber", "1.0 telegraph") == 301
    assert grams("hamburger buns", "4.0 halved") == 176
    assert grams("corn tortillas", "8.0 6-inch") == 208
    assert grams("celery", "2.0 rib") == 80
    assert grams("crème fraîche", "250.0 ml") > 240
    result = mod.ingredient_weight_tool_usda.invoke(
        {"ingredient_names": ["salt", "olive oil"], "measurements": ["1.0", "1.5"], "return_details": True, "debug": True}
    )
    assert result["weights"] == [0.3, mod.BLANK_OIL_GRAMS]


def test_size_word_units_and_slice_ambiguity():
    assert round(grams("chicken thighs", "4.0 small"), 1) == 501.8
    assert grams("baguette bread", "12.0 whole") == 300  # slices, not 12 loaves
    assert grams("baguette", "1.0") == 250
    assert round(grams("honey", "1 dstspn"), 1) == 14.2


def test_lookalike_names_are_not_captured_by_reviewed_rows():
    assert grams("squash blossoms", "12.0") == 60  # not 12 whole squash
    assert grams("corn husks", "15.0") == 30  # not 15 corn ears
    assert grams("corn tacos", "12.0") == 156
    assert grams("broccoli", "3.0 stalk") == 453
    assert grams("broccoli", "3.5 floret") == 105  # florets are 30 g each, a head is 300 g
    with patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled", None)):
        assert grams("Chickpeas", "3.0") == 0  # container-implied count stays unresolved


def test_common_spoon_foods_use_density_not_the_generic_guess():
    assert round(grams("baking soda", "1 tsp"), 1) == 4.6
    assert round(grams("miso paste", "1 tbsp"), 1) == 17.2
    assert round(grams("orange marmalade", "8 tbsp"), 1) == 160.0


def test_blank_lines_never_take_a_whole_item_weight():
    result = mod.ingredient_weight_tool_usda.invoke(
        {
            "ingredient_names": ["yoghurt", "broccoli", "baguette", "iceberg lettuce", "cucumber", "turnip", "bacon"],
            "measurements": [""] * 7,
            "return_details": True,
            "debug": True,
        }
    )
    assert result["weights"] == [mod.BLANK_DEFAULT_GRAMS] * 7


def test_cup_table_lookalikes_from_the_full_corpus_diff():
    assert grams("salad potatoes", "2.0 cups") != 56  # potato salad is not salad leaves
    assert round(grams("split peas", "1.0 cup"), 0) != 134
    assert round(grams("ricotta cheese", "1 cup"), 0) != 112
    assert round(grams("mustard greens", "1 cup"), 0) != 249


def test_chicken_wing_uses_edible_weight_not_bone_in_piece():
    assert grams("chicken wings", "10.0 medium") == 410
