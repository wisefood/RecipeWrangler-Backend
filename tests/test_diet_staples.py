"""Owner decision 7 Nov 2026: distilled spirits and plain staples count as suitable by name."""
import re
import pytest
from recipe_wrangler.utils.consumer_suitability import PLANT_STAPLE_PATTERNS


def staple(name):
    return any(re.fullmatch(p, name, re.IGNORECASE) for p in PLANT_STAPLE_PATTERNS)


@pytest.mark.parametrize("name", [
    "tequila", "agave tequila", "blanco tequila", "rum", "spiced rum", "vodka", "gin", "whiskey", "whisky",
    "bourbon", "brandy", "cognac", "mezcal", "dark rum", "white rum",
    "pasta", "spaghetti", "penne", "dried pasta", "whole wheat spaghetti", "macaroni", "orzo", "fusilli",
    "bread", "white bread", "brown bread", "wholemeal bread", "sourdough bread", "seeded bread", "rye bread",
])
def test_named_staples_are_suitable(name):
    assert staple(name)


@pytest.mark.parametrize("name", [
    "orange liqueur", "melon liqueur", "Baileys Irish Cream", "Kahlua", "Amaretto", "Southern Comfort",
    "sweet white vermouth", "grenadine", "orange bitters",
    "egg pasta", "fresh pasta", "ricotta ravioli", "pasta with chicken", "spaghetti bolognese",
    "garlic bread", "cheese bread", "milk bread", "honey bread", "butter bread", "rum cake", "rum and raisin ice cream",
    "mayonnaise", "frozen cut broccoli in cheese sauce",
])
def test_formulation_dependent_products_stay_unknown(name):
    assert not staple(name)


@pytest.mark.parametrize("name", ["garlic bread", "carrot cake", "onion beef", "lemon cake", "banana bread", "tomato beef"])
def test_bread_cake_beef_after_a_plant_food_is_a_different_food(name):
    assert not staple(name)


@pytest.mark.parametrize("name", ["bread flour", "cake flour", "beef tomato", "beef tomatoes", "flour", "tomatoes"])
def test_leading_modifiers_still_work(name):
    assert staple(name)
