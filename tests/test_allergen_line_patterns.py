"""Patterns found by re-reading allergens from original recipe lines (7 November 2026)."""
import pytest
from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names as detect


def states(line):
    return {(e["allergen"], e["presence"]) for e in detect([line])}


@pytest.mark.parametrize("line,allergen", [
    ("4 cups chopped vegetables such as onion, carrot, and celery", "celery"),
    ("1 tablespoon oil such as canola, rice bran, sesame", "sesame"),
    ("1 cup salad vegetables, for example celery and cucumber", "celery"),
])
def test_example_lists_are_possible_not_definite(line, allergen):
    found = {a: p for a, p in states(line)}
    assert found.get(allergen) == "possible"


def test_named_head_food_stays_definite_with_example_tail():
    assert ("tree_nut", "contains") in states("40g almonds, such as marcona")
    assert ("sesame", "contains") in states("2 tablespoons sesame oil, such as toasted")


@pytest.mark.parametrize("line", [
    "200g assorted Asian mushrooms such as shiitake and oyster, wiped clean",
    "50 g Mushrooms, oyster, raw",
    "1 cup oyster mushrooms",
])
def test_oyster_mushroom_is_not_a_mollusc(line):
    assert not {a for a, _ in states(line) if a == "molluscs"}


def test_real_oysters_are_still_molluscs():
    assert ("molluscs", "contains") in states("12 fresh oysters")


@pytest.mark.parametrize("line", [
    "4 tablespoons chickpea garbanzo flour",
    "1/2 cup garbanzo flour",
])
def test_chickpea_flour_spelling_is_gluten_free(line):
    assert not {a for a, _ in states(line) if a in {"gluten", "wheat"}}


@pytest.mark.parametrize("line", [
    "1 large courgette zucchini , cut into thin noodles",
    "3 courgette, spiralised into noodles",
    "2 zucchini noodles",
    "200g carrot noodles",
])
def test_vegetable_noodles_are_not_wheat(line):
    assert not {a for a, _ in states(line) if a in {"gluten", "wheat"}}


@pytest.mark.parametrize("line", [
    "250g egg noodles",
    "200g noodles for the carrot soup",
    "100g wheat noodles with courgette",
])
def test_real_noodles_stay_wheat(line):
    assert ("gluten", "contains") in states(line)


def test_oyster_sauce_with_mushrooms_is_still_a_mollusc():
    assert any(a == "molluscs" for a, _ in states("200g mushrooms, 2 tablespoons oyster sauce"))


def test_vege_noodle_heading_is_not_wheat():
    assert not {a for a, _ in states("Vege noodle salad") if a in {"gluten", "wheat"}}


@pytest.mark.parametrize("line", [
    "2 teaspoons cornflour cornstarch (check that it does not contain wheat)",
    "1 tablespoon cornflour (doesn't contain gluten)",
])
def test_statement_that_a_food_does_not_contain_wheat_is_not_wheat(line):
    assert not {a for a, _ in states(line) if a in {"gluten", "wheat"}}


@pytest.mark.parametrize("line", [
    "50g nuts of your choice, roasted (walnuts, pine nuts, hazelnuts)",
    "2 Tablespoons chopped nuts (try walnuts or pecans)",
    "100g mixed nuts, e.g. hazelnuts, walnuts, almonds",
])
def test_nuts_with_named_examples_stay_definite_for_allergy_filters(line):
    assert ("tree_nut", "contains") in states(line)


def test_plain_peanut_butter_stays_definite():
    assert ("peanut", "contains") in states("2 tablespoons peanut butter")


def test_tapioca_starch_flour_is_gluten_free():
    assert not {a for a, _ in states("1 cup tapioca starch/flour") if a in {"gluten", "wheat"}}


@pytest.mark.parametrize("line", [
    "1 tablespoon mirin (check that it is gluten-free as some can contain wheat)",
    "1 tablespoon stock (might contain wheat)",
])
def test_can_contain_warning_is_possible_not_definite(line):
    found = states(line)
    assert ("wheat", "contains") not in found and ("gluten", "contains") not in found
