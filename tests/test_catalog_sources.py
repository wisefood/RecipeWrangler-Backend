"""Tests for the unified source/course-type registry."""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog import sources as S


class TestSourceResolution:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("FoodHero", "foodhero"),
            ("foodhero", "foodhero"),
            ("  FOODHERO  ", "foodhero"),
            ("Curated Irish Recipes", "irish_safefood"),
            ("safefood", "irish_safefood"),
            ("irish safefood", "irish_safefood"),
            ("irish-safefood", "irish_safefood"),
            ("HealthyFoods", "healthyfoods"),
            ("Healthy Food Guide", "healthyfoods"),
            ("MyPlate", "myplate"),
            ("recipe1m", "recipe1m"),
        ],
    )
    def test_every_known_spelling_resolves(self, value, expected):
        assert S.slug_for(value) == expected

    def test_unknown_source_resolves_to_none(self):
        assert S.resolve("not-a-source") is None
        assert S.slug_for("not-a-source") is None
        assert S.raw_for("not-a-source") is None

    def test_none_and_empty_are_safe(self):
        for value in (None, "", "   "):
            assert S.resolve(value) is None

    def test_slug_and_raw_round_trip(self):
        for source in S.SOURCES:
            assert S.raw_for(source.slug) == source.raw
            assert S.slug_for(source.raw) == source.slug


class TestSourceProperties:
    def test_all_live_corpus_sources_are_registered_for_rebuilds(self):
        assert {source.raw for source in S.active_sources()} >= {
            "Best of Hungary",
            "Curated Hungarian Recipes",
            "Curated Irish Recipes",
            "Curated Slovenian Recipes",
            "FoodHero",
            "HealthyFoods",
            "Irish Heart Foundation",
            "MyPlate",
            "Slovenian Kitchen",
            "SuperValu",
            "The Hungary Soul",
        }

    def test_curated_and_trusted_sets(self):
        assert S.curated_slugs() == {
            "foodhero",
            "healthyfoods",
            "hungarian",
            "irish_safefood",
            "slovenian",
        }
        assert S.trusted_slugs() == {"foodhero", "healthyfoods", "irish_safefood"}

    def test_recipe1m_is_retired_and_excluded_from_active(self):
        recipe1m = S.by_slug("recipe1m")
        assert recipe1m is not None and recipe1m.retired
        assert "recipe1m" not in {s.slug for s in S.active_sources()}

    def test_ground_truth_nutrition_sources_are_ordered(self):
        assert S.ground_truth_nutrition_sources("HealthyFoods") == (
            "healthyfoods_original",
            "healthyfoods",
        )
        assert S.ground_truth_nutrition_sources("Curated Irish Recipes") == (
            "safefood_rcsi",
            "safefood_web",
            "safefood",
        )
        assert S.ground_truth_nutrition_sources("recipe1m") == ()
        assert S.ground_truth_nutrition_sources("MyPlate") == ("myplate",)
        assert S.ground_truth_nutrition_sources("Curated Hungarian Recipes") == (
            "planeat",
        )
        assert S.ground_truth_nutrition_sources("Curated Slovenian Recipes") == (
            "slovenian_original",
        )

    def test_rank_orders_curated_above_the_rest_and_retired_last(self):
        assert S.source_rank("FoodHero") < S.source_rank("hungarian")
        assert S.source_rank("hungarian") < S.source_rank("MyPlate")
        assert S.source_rank("MyPlate") < S.source_rank("recipe1m")
        assert S.source_rank("recipe1m") < S.source_rank("unknown")

    def test_myplate_has_no_dangling_collection_urn(self):
        """nutrition_postgres resolves MyPlate to urn:rcollection:myplate, which
        does not exist in the catalog. The registry must not repeat that."""
        assert S.collection_urn_for("MyPlate") is None

    def test_known_collection_urns(self):
        assert S.collection_urn_for("FoodHero") == "urn:rcollection:foodhero"
        assert S.collection_urn_for("HealthyFoods") == "urn:rcollection:healthyfood"
        assert (
            S.collection_urn_for("Curated Irish Recipes")
            == "urn:rcollection:rcsi-recipes"
        )


class TestCourseTypeCanonicalization:
    @pytest.mark.parametrize(
        "variant,canonical",
        [
            ("main-dish", "main-dish"),
            ("main_dish", "main-dish"),
            ("main dish", "main-dish"),
            ("lunch", "main-dish"),
            ("dinner", "main-dish"),
            ("dessert", "desserts"),
            ("desserts", "desserts"),
            ("snack", "snacks"),
            ("snacks", "snacks"),
            ("beverage", "beverages"),
            ("beverages", "beverages"),
            ("breakfast", "breakfast"),
            ("soup", "soup"),
            ("salad", "salad"),
        ],
    )
    def test_variants_fold_onto_canonical(self, variant, canonical):
        assert S.canonical_course_type(variant) == canonical

    def test_canonical_values_are_stable_under_reapplication(self):
        for course in S.COURSE_TYPES:
            assert S.canonical_course_type(course) == course

    def test_pasta_is_not_a_course_type(self):
        """'Pasta' is a dish family, not a course. The Browse-by-Category UI
        conflates the two; the registry must not."""
        assert S.canonical_course_type("pasta") is None

    def test_unknown_and_empty_values(self):
        assert S.canonical_course_type("nonsense") is None
        assert S.canonical_course_type(None) is None
        assert S.canonical_course_type("") is None

    def test_collection_canonicalization_dedupes_and_preserves_order(self):
        assert S.canonical_course_types(
            ["main_dish", "lunch", "dessert", "dessert", "nonsense"]
        ) == ["main-dish", "desserts"]

    def test_collection_canonicalization_handles_empty(self):
        assert S.canonical_course_types([]) == []
        assert S.canonical_course_types(None) == []
