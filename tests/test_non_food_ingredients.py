from recipe_wrangler.utils.non_food_ingredients import is_unambiguous_non_food_ingredient


def test_unambiguous_equipment_is_detected() -> None:
    for value in ("**aluminum foil**", "paper cups", "wooden sticks", "baking trays"):
        assert is_unambiguous_non_food_ingredient(value)


def test_food_is_not_detected_as_equipment() -> None:
    for value in ("olive oil", "baking soda", "wooden spoonful of honey"):
        assert not is_unambiguous_non_food_ingredient(value)
