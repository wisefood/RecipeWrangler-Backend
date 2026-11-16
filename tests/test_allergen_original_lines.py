"""Allergens read from original lines for live analysis (AD-01) and white chocolate (AD-02)."""
import pytest
from recipe_wrangler.api.routers import recipes
from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names as detect
from recipe_wrangler.utils.line_allergens import extra_line_evidence, is_heading


def contains(line):
    return {e["allergen"] for e in detect([line]) if e["presence"] == "contains"}


def test_soy_milk_name_loses_soy_but_the_line_keeps_it():
    fields = recipes._analysis_allergen_fields(["milk"], ["150 g Milk, soya, non-dairy alternative to milk"])
    assert "soy" in fields["allergens"]
    row = next(r for r in fields["allergen_evidence"] if r["allergen"] == "soy")
    assert row["ingredient"].startswith("150 g Milk, soya")


def test_line_evidence_only_adds_never_replaces():
    names_only = recipes._analysis_allergen_fields(["eggs", "flour"])
    with_lines = recipes._analysis_allergen_fields(["eggs", "flour"], ["2 eggs", "100 g flour"])
    assert set(names_only["allergens"]) == set(with_lines["allergens"])
    assert len(with_lines["allergen_evidence"]) == len(names_only["allergen_evidence"])


def test_no_original_lines_behaves_as_before():
    assert recipes._analysis_allergen_fields(["milk"]) == recipes._analysis_allergen_fields(["milk"], None)
    assert recipes._analysis_allergen_fields(["milk"]) == recipes._analysis_allergen_fields(["milk"], [])


def test_headings_are_ignored():
    assert is_heading("Pastry") and is_heading("For the dressing")
    assert not is_heading("2 eggs") and not is_heading("celery")
    assert extra_line_evidence(set(), ["Noodle salad"]) == []


@pytest.mark.parametrize("line", ["8 oz white chocolate", "200g white chocolate chips", "1/2 cup white chocolate chunks"])
def test_white_chocolate_contains_milk(line):
    assert "milk" in contains(line)


@pytest.mark.parametrize("line", [
    "dairy-free white chocolate", "vegan white chocolate chips", "white chocolate (dairy-free)", "1 cup chocolate chips",
])
def test_dairy_free_white_chocolate_and_plain_chocolate_are_not_milk(line):
    assert "milk" not in contains(line)


@pytest.mark.parametrize("line", [
    "4 low calorie hot dog buns, split", "12 ounces prepared pizza dough", "1 pretzel", "2 cups crushed pretzels",
    "1 box cake mix", "2 croissants", "1 cup panko", "1 English muffin", "croutons",
])
def test_plain_wheat_products_are_gluten(line):
    assert "gluten" in contains(line)


@pytest.mark.parametrize("line", ["gluten-free pizza dough", "gluten free buns", "1 cup rice noodles"])
def test_gluten_free_wheat_products_are_not_gluten(line):
    assert "gluten" not in contains(line)


def evidence(line):
    return {(e["allergen"], e["presence"]) for e in detect([line])}


@pytest.mark.parametrize("line,allergen", [
    ("1/2 cup dry white wine", "sulphites"),
    ("1 cup red wine", "sulphites"),
    ("1 teaspoon Worcestershire sauce", "fish"),
    ("2 tablespoons worcester sauce", "fish"),
    ("3 Tbsp. mayonnaise", "egg"),
    ("1/2 cup KRAFT Real Mayo Mayonnaise", "egg"),
    ("2 tablespoons light mayo", "egg"),
])
def test_policy_items_are_definite(line, allergen):
    assert (allergen, "contains") in evidence(line)


@pytest.mark.parametrize("line,allergen", [
    ("2 tablespoons red wine vinegar", "sulphites"),
    ("1/4 cup rice wine", "sulphites"),
    ("1/2 cup dried apricots", "sulphites"),
    ("1/4 cup raisins", "sulphites"),
    ("1 tablespoon mirin", "sulphites"),
    ("1 tablespoon caesar dressing", "fish"),
    ("2 tablespoons aioli", "egg"),
])
def test_other_items_stay_possible(line, allergen):
    assert (allergen, "possible") in evidence(line)
    assert (allergen, "contains") not in evidence(line)


@pytest.mark.parametrize("line,allergen", [
    ("1 tablespoon vegan Worcestershire sauce", "fish"),
    ("3 tablespoons vegan mayonnaise", "egg"),
    ("3 tablespoons egg-free mayo", "egg"),
    ("2 tablespoons eggless mayonnaise", "egg"),
    ("1 cup alcohol-free sparkling grape juice", "sulphites"),
])
def test_vegan_and_free_from_versions_are_not_definite(line, allergen):
    assert (allergen, "contains") not in evidence(line)


def test_wine_without_added_sulphites_stays_possible():
    assert ("sulphites", "possible") in evidence("no added sulphites wine")
    assert ("sulphites", "contains") not in evidence("no added sulphites wine")


@pytest.mark.parametrize("line", ["3 tablespoons vinegar (red wine)", "2 tbsp vinegar, white wine"])
def test_vinegar_named_with_its_wine_is_not_definite(line):
    assert ("sulphites", "contains") not in evidence(line)
    assert ("sulphites", "possible") in evidence(line)
