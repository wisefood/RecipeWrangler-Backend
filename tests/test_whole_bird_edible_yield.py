from recipe_wrangler.tools import ingredient_weight_tool as mod


def weigh(name, measurement):
    result = mod.ingredient_weight_tool_usda.invoke(
        {"ingredient_names": [name], "measurements": [measurement], "return_details": True, "debug": True}
    )
    return result["weights"][0], result["details"][0]


def test_whole_chicken_mass_stays_gross_for_the_with_bone_record():
    # The matcher prices whole chicken with "Chicken, whole, raw, meat and skin, weighed with bone" (reviewed 2026-11-06).
    grams, detail = weigh("whole chicken", "1.5 kg")

    assert grams == 1500
    assert detail["match_type"] == "direct_mass"


def test_whole_skinless_chicken_uses_meat_only_yield_and_count_reference():
    grams, detail = weigh("whole skinless chicken", "1.0 whole")

    assert abs(grams - 1600 * 0.434) < 0.01
    assert detail["edible_yield_factor"] == 0.434


def test_bird_parts_and_plain_chicken_are_not_scaled():
    assert weigh("chicken", "1.0 kg")[0] == 1000
    assert weigh("whole chicken breasts", "1.0 kg")[0] == 1000


def test_whole_duck_count_reference_then_edible_yield():
    grams, detail = weigh("whole duck", "1.0 whole")

    assert abs(grams - 2000 * 0.633) < 0.01


def test_bare_count_of_whole_bird_uses_whole_reference():
    grams, detail = weigh("whole chicken", "1.0")

    assert grams == 1600
