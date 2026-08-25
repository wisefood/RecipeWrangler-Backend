"""Entity-layer document derivation.

``validate()`` is the one place a recipe document is assembled, so these are the
rules that every write path — single or bulk — is held to. None of this needs a
cluster: the entities are constructed with an explicit alias and
``register=False`` so they never touch Elasticsearch or the global registry.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog.entities import (
    DEFAULT_SCORE_REGION_ORDER,
    NUTRI_RANKS,
    Recipe,
    RecipeProfile,
    normalize_title,
    nutri_label,
    nutri_rank,
)
from recipe_wrangler.catalog.entity import ValidationError, urn_type


@pytest.fixture
def recipe():
    return Recipe(alias="test_recipes", register=False)


@pytest.fixture
def profile():
    return RecipeProfile(alias="test_profiles", register=False)


def build(recipe, **overrides):
    doc = {"urn": "urn:recipe:r1", "title": "Test Recipe"}
    doc.update(overrides)
    return recipe.validate(doc)


class TestScalarHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [("Nutriscore_A", "A"), ("a", "A"), ("A", "A"), ("nutriscore_e", "E")],
    )
    def test_nutri_label_normalizes(self, value, expected):
        assert nutri_label(value) == expected

    @pytest.mark.parametrize("value", ["", None, "Nutriscore_Z", "banana", "F"])
    def test_nutri_label_rejects_nonsense(self, value):
        assert nutri_label(value) is None

    def test_nutri_rank_orders_a_best(self):
        assert nutri_rank("A") < nutri_rank("C") < nutri_rank("E")
        assert nutri_rank("bogus") is None

    def test_ranks_cover_every_label(self):
        assert set(NUTRI_RANKS) == {"A", "B", "C", "D", "E"}

    def test_normalize_title_folds_accents_and_punctuation(self):
        assert normalize_title("  Crème Brûlée!! ") == "creme brulee"

    def test_normalize_title_keeps_non_latin_letters(self):
        assert normalize_title("Σουβλάκι") != ""

    def test_urn_type_rejects_malformed(self):
        assert urn_type("urn:recipe:x") == "recipe"
        for bad in ("recipe:x", "urn:recipe", "", None):
            with pytest.raises(ValidationError):
                urn_type(bad)


class TestRecipeValidation:
    def test_title_is_required(self, recipe):
        with pytest.raises(ValidationError):
            recipe.validate({"urn": "urn:recipe:r1", "title": "   "})

    def test_recipe_id_derived_from_urn(self, recipe):
        assert build(recipe)["recipe_id"] == "r1"

    def test_source_resolved_to_raw_with_registry_metadata(self, recipe):
        doc = build(recipe, source="foodhero")
        assert doc["source"] == "FoodHero"
        assert doc["source_name"] == "Food Hero"
        assert doc["collection_urn"] == "urn:rcollection:foodhero"
        assert doc["source_rank"] == 0

    def test_unknown_source_still_gets_a_rank(self, recipe):
        doc = build(recipe, source="mystery")
        assert doc["source_rank"] == 99

    def test_myplate_gets_no_dangling_collection(self, recipe):
        assert "collection_urn" not in build(recipe, source="MyPlate")

    def test_course_types_canonicalized_from_legacy_dish_types(self, recipe):
        doc = build(recipe, dish_types=["main_dish", "lunch", "dinner"])
        assert doc["course_types"] == ["main-dish"]

    def test_dish_types_never_written(self, recipe):
        """One concept, one field. `dish_types` was the v2 name holding
        byte-identical values; two fields for one concept is how main-dish and
        main_dish drifted into separate buckets."""
        doc = build(recipe, course_types=["dessert"])
        assert doc["course_types"] == ["desserts"]
        assert "dish_types" not in doc

    def test_incoming_dish_types_are_absorbed_not_echoed(self, recipe):
        """Legacy input is still accepted — it just lands in course_types."""
        doc = build(recipe, dish_types=["main_dish"])
        assert doc["course_types"] == ["main-dish"]
        assert "dish_types" not in doc

    def test_unknown_course_types_dropped_entirely(self, recipe):
        doc = build(recipe, course_types=["pasta"])
        assert "course_types" not in doc and "dish_types" not in doc

    def test_scalar_course_type_accepted(self, recipe):
        assert build(recipe, course_types="soup")["course_types"] == ["soup"]

    def test_stored_main_dish_is_not_guessed_away_from_the_title(self, recipe):
        doc = build(recipe, title="Basic tomato sauce", course_types=["main-dish"])
        assert doc["course_types"] == ["main-dish"]

    def test_has_image_reflects_url_presence(self, recipe):
        assert build(recipe, image_url="http://x/y.jpg")["has_image"] is True
        assert build(recipe, image_url="  ")["has_image"] is False
        assert build(recipe)["has_image"] is False

    def test_every_write_is_stamped_as_schema_v4(self, recipe):
        assert build(recipe)["schema_version"] == 4

    def test_convenience_is_derived_on_every_write(self, recipe):
        doc = build(recipe, duration=30, ingredients=["a", "b", "c", "d", "e"])
        assert doc["convenience"] == ["quick", "simple"]

    def test_stale_convenience_is_replaced(self, recipe):
        doc = build(recipe, duration=60, ingredients=[str(i) for i in range(6)], convenience=["quick"])
        assert doc["convenience"] == []


class TestIngredientNormalization:
    def test_mixed_string_and_dict_entries(self, recipe):
        doc = build(
            recipe,
            ingredients=[{"name": "Pasta", "quantity": 200, "unit": "g"}, "Tomato"],
        )
        assert doc["ingredient_names"] == ["pasta", "tomato"]
        assert doc["ingredient_count"] == 2
        assert doc["ingredients"][0]["position"] == 0
        assert doc["ingredients"][1]["position"] == 1

    def test_names_deduplicated_but_lines_preserved(self, recipe):
        """Two lines can legitimately name the same ingredient; the flattened
        name list is for filtering, the line count is for display."""
        doc = build(recipe, ingredients=["Tomato", "Tomato"])
        assert doc["ingredient_names"] == ["tomato"]
        assert doc["ingredient_count"] == 2

    def test_blank_entries_discarded(self, recipe):
        doc = build(recipe, ingredients=["Onion", "", None, {"name": "  "}])
        assert doc["ingredient_names"] == ["onion"]
        assert doc["ingredient_count"] == 1


class TestDefaultScoreSelection:
    """A recipe showing C in a list and A on its detail page is what happens
    when each view picks its own region. One field, one rule."""

    def test_ground_truth_profile_wins_over_pipeline_regions(self, recipe):
        doc = build(
            recipe,
            nutri_score_eu="Nutriscore_C",
            profiles=[
                {"region": "eu", "nutri_score": "C", "is_ground_truth": False},
                {"region": "ie", "nutri_score": "A", "is_ground_truth": True},
            ],
        )
        assert doc["default_nutri_score"] == "A"
        assert doc["default_nutri_rank"] == 1

    def test_eu_profile_used_when_no_ground_truth(self, recipe):
        doc = build(
            recipe,
            profiles=[
                {"region": "hu", "nutri_score": "D", "is_ground_truth": False},
                {"region": "eu", "nutri_score": "B", "is_ground_truth": False},
            ],
        )
        assert doc["default_nutri_score"] == "B"

    def test_falls_back_to_flat_region_fields_in_fixed_order(self, recipe):
        doc = build(recipe, nutri_score_ie="Nutriscore_D", nutri_score_eu="Nutriscore_B")
        assert doc["default_nutri_score"] == "B", "EU must win over IE"

    def test_non_eu_region_used_when_eu_absent(self, recipe):
        assert build(recipe, nutri_score_hu="Nutriscore_D")["default_nutri_score"] == "D"

    def test_absent_when_nothing_scoreable(self, recipe):
        doc = build(recipe)
        assert "default_nutri_score" not in doc
        assert "default_nutri_rank" not in doc

    def test_existing_default_preserved_and_normalized(self, recipe):
        doc = build(recipe, default_nutri_score="Nutriscore_C")
        assert doc["default_nutri_score"] == "C"
        assert doc["default_nutri_rank"] == 3

    def test_rank_always_matches_label(self, recipe):
        for label in NUTRI_RANKS:
            doc = build(recipe, nutri_score_eu=label)
            assert doc["default_nutri_rank"] == NUTRI_RANKS[doc["default_nutri_score"]]

    def test_region_order_is_deterministic_and_eu_first(self):
        assert DEFAULT_SCORE_REGION_ORDER[0] == "eu"
        assert DEFAULT_SCORE_REGION_ORDER == ("eu", "ie", "hu", "slovenian")
        assert len(set(DEFAULT_SCORE_REGION_ORDER)) == len(DEFAULT_SCORE_REGION_ORDER)


class TestProfileValidation:
    def test_grain_fields_required(self, profile):
        with pytest.raises(ValidationError):
            profile.validate({"recipe_id": "r1"})
        with pytest.raises(ValidationError):
            profile.validate({"nutrition_source": "eu"})

    def test_parent_urn_derived(self, profile):
        doc = profile.validate({"recipe_id": "r1", "nutrition_source": "eu"})
        assert doc["recipe_urn"] == "urn:recipe:r1"

    def test_urn_encodes_the_grain(self, profile):
        assert profile.urn_for("r1", "eu") == "urn:recipe_profile:r1:eu"

    def test_ground_truth_flag_from_registry(self, profile):
        gt = profile.validate(
            {
                "recipe_id": "r1",
                "nutrition_source": "safefood_rcsi",
                "source": "Curated Irish Recipes",
            }
        )
        assert gt["is_ground_truth"] is True
        derived = profile.validate(
            {"recipe_id": "r1", "nutrition_source": "eu", "source": "Curated Irish Recipes"}
        )
        assert derived["is_ground_truth"] is False

    def test_recipe1m_original_is_not_supported_ground_truth(self, profile):
        """The retired source and its nutrition no longer enter the catalog."""
        doc = profile.validate(
            {
                "recipe_id": "x",
                "nutrition_source": "recipe1m_original",
                "source": "recipe1m",
            }
        )
        assert doc["is_ground_truth"] is False

    def test_score_label_normalized_with_rank(self, profile):
        doc = profile.validate(
            {
                "recipe_id": "r1",
                "nutrition_source": "eu",
                "nutri_score": {"label": "Nutriscore_B", "points": 2.0},
            }
        )
        assert doc["nutri_score"]["label"] == "B"
        assert doc["nutri_score"]["rank"] == 2
        assert doc["nutri_score"]["points"] == 2.0
