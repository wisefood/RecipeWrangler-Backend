from recipe_wrangler.tools.parse_recipe_tool import (
    _leading_quantity_value_and_end,
    restore_sibling_quantity,
)

SPREAD = ["125g reduced-fat spread", "1 tablespoon reduced-fat spread", "2 eggs"]


def test_quantity_copied_from_a_line_with_another_unit_is_restored():
    assert restore_sibling_quantity("125.0 tablespoon", SPREAD[1], SPREAD) == "1.0 tablespoon"
    assert restore_sibling_quantity(
        "400.0 tablespoons", "2 tablespoons fresh basil",
        ["400g can chopped tomatoes with basil", "2 tablespoons fresh basil"],
    ) == "2.0 tablespoon"
    assert restore_sibling_quantity(
        "80.0 teaspoon", "1 teaspoon reduced-fat spread",
        ["80g reduced-fat spread, melted, cooled", "1 teaspoon reduced-fat spread"],
    ) == "1.0 teaspoon"


def test_correct_or_unexplained_quantities_are_left_alone():
    assert restore_sibling_quantity("1.0 tablespoon", SPREAD[1], SPREAD) is None
    # 7 matches no other line: not a sibling copy, so no guess is made
    assert restore_sibling_quantity("7.0 tablespoon", SPREAD[1], SPREAD) is None
    # the sibling line has the same unit: that is a sum or a duplicate, not a unit mix-up
    assert restore_sibling_quantity(
        "2.0 cup", "⅓ cup sugar", ["2 cups sugar", "⅓ cup sugar"]
    ) is None
    # ranges and alternatives stay as parsed
    assert restore_sibling_quantity("125.0 tablespoon", "1 to 2 tablespoon spread", SPREAD) is None


def test_superscript_fractions_are_read():
    assert _leading_quantity_value_and_end("¹⁄³ cup raw almonds")[0] == 1 / 3
    assert _leading_quantity_value_and_end("1¹/³ cups flour")[0] == 1 + 1 / 3
    assert _leading_quantity_value_and_end("²/³ cup skim milk")[0] == 2 / 3


def test_superscript_fraction_line_corrects_the_whole_number_the_model_read():
    from recipe_wrangler.tools.parse_recipe_tool import restore_superscript_fraction_quantity as fix

    assert fix("1.0 cup", "¹⁄³ cup thinly sliced jalapeños", "jalapenos") == "0.33 cup"
    assert fix("2.0 cup", "²⁄³ cup almond milk", "almond milk") == "0.67 cup"
    assert fix("1.0 cup", "½ cup panko, plus ¹⁄³ extra", "panko") is None
    assert fix("0.33 cup", "⅓ cup skim milk", "skim milk") is None
