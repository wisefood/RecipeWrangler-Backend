from recipe_wrangler.utils.non_food_ingredients import is_unambiguous_non_food_ingredient


def test_unambiguous_equipment_is_detected() -> None:
    for value in (
        "**aluminum foil**", "paper cups", "wooden sticks", "baking trays",
        "frying pan", "twine", "kitchen string", "string or twine",
        "stainless steel turner", "egg moulds", "egg mold",
        "muffin or bun tray", "flan tin", "pastry cutter", "paper patty-pan cases", "paper patty cake cases",
        "greaseproof paper", "cooking twine", "coloured paper cones",
        "paper or plastic bags", "bamboo sticks",
        "muffin cups", "cupcake containers", "zip-lock bags", "jam jelly jars", "craft pegs",
        "pipe cleaners", "craft animal eyes", "rolling pin", "ribbon",
    ):
        assert is_unambiguous_non_food_ingredient(value)


def test_food_is_not_detected_as_equipment() -> None:
    for value in ("olive oil", "baking soda", "wooden spoonful of honey",
                  "bamboo shoots", "paper-thin prosciutto", "tin of tomatoes", "cooking chocolate",
                  "ribbon candy sprinkles", "2 tablespoons butter", "cupcake mix", "tablespoons"):
        assert not is_unambiguous_non_food_ingredient(value)
