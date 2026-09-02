from __future__ import annotations

import pandas as pd
import pytest

from recipe_wrangler.pricing.constants import PROCESSED_DIR
from recipe_wrangler.pricing.cost_calculator import (
    CostCatalogue,
    calculate_cost_from_profile,
    calculate_recipe_batch,
    calculate_recipe_cost_profile,
    load_cost_catalogue,
)
from recipe_wrangler.pricing.recipe_cost_categories import RecipeCostCalibration


def _catalogue() -> CostCatalogue:
    common = {
        "food_category": "meat",
        "global_cost_tier": "€€€",
        "within_category_position": "Middle",
        "parent_within_category_position": None,
        "price_evidence_confidence": "Medium",
        "cost_reference_version": "test-v1",
    }
    frame = pd.DataFrame(
        [
            {
                **common,
                "product_id": "base__chicken",
                "source_ingredient_id": "chicken",
                "canonical_name": "chicken",
                "product_detail": "",
                "product_level": "base",
                "eu_reference_price_eur_kg": 6.0,
                "price_ie_eur_kg": 7.0,
                "price_hu_eur_kg": 5.0,
                "price_si_eur_kg": 5.5,
            },
            {
                **common,
                "product_id": "detail__chicken_breast_fillet",
                "source_ingredient_id": "chicken_breast_fillet",
                "canonical_name": "chicken",
                "product_detail": "breast fillet",
                "product_level": "detail",
                "eu_reference_price_eur_kg": 8.0,
                "price_ie_eur_kg": 9.0,
                "price_hu_eur_kg": 7.0,
                "price_si_eur_kg": 7.5,
            },
            {
                **common,
                "product_id": "base__beef",
                "source_ingredient_id": "beef",
                "canonical_name": "beef",
                "product_detail": "",
                "product_level": "base",
                "eu_reference_price_eur_kg": 9.0,
                "price_ie_eur_kg": 10.0,
                "price_hu_eur_kg": 8.0,
                "price_si_eur_kg": 8.5,
            },
            {
                **common,
                "product_id": "detail__beef_minced_meat",
                "source_ingredient_id": "beef_minced_meat",
                "canonical_name": "beef",
                "product_detail": "minced meat",
                "product_level": "detail",
                "eu_reference_price_eur_kg": 10.0,
                "price_ie_eur_kg": 11.0,
                "price_hu_eur_kg": 9.0,
                "price_si_eur_kg": 9.5,
            },
        ]
    )
    return CostCatalogue(
        frame,
        aliases={
            "chicken wings": "base__chicken",
            "ground beef": "detail__beef_minced_meat",
        },
    )


def test_resolver_prefers_exact_detail_over_base() -> None:
    match = _catalogue().resolve("chicken breast fillet", "IE")
    assert match["matched_product_id"] == "detail__chicken_breast_fillet"
    assert match["price_scope"] == "detailed_product"
    assert match["economic_reference_price_eur_kg"] == pytest.approx(9.0)
    assert match["match_method"] == "exact_detail:ingredient_name"


def test_base_product_supports_exact_plural() -> None:
    match = _catalogue().resolve("chickens", "EU")
    assert match["matched_product_id"] == "base__chicken"
    assert match["economic_reference_price_eur_kg"] == pytest.approx(6.0)
    assert match["price_scope"] == "base_product"


def test_reviewed_alias_uses_general_base_price_for_chicken_wings() -> None:
    match = _catalogue().resolve("chicken wings", "SI")
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"] == "reviewed_alias:ingredient_name"
    assert match["cost_match_confidence"] == "medium"
    assert match["economic_reference_price_eur_kg"] == pytest.approx(5.5)
    assert "median of available details" in match["mapping_explanation"]


def test_reviewed_alias_can_match_inside_a_safe_descriptive_phrase() -> None:
    match = _catalogue().resolve("(1lb) boneless chicken wings, skin on", "EU")
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"] == "reviewed_alias_phrase:ingredient_name"
    assert match["cost_match_confidence"] == "medium"


@pytest.mark.parametrize(
    "name",
    [
        "chicken stock with chicken wings",
        "veal cutlets or chicken wings",
    ],
)
def test_reviewed_alias_phrase_rejects_unsafe_context_or_alternatives(
    name: str,
) -> None:
    assert _catalogue().resolve(name, "EU")["match_status"] == "unmatched"


def test_phrase_fallback_is_traceable() -> None:
    match = _catalogue().resolve("boneless chicken pieces", "EU")
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"] == "base_phrase_fallback:ingredient_name"
    assert match["cost_match_confidence"] == "low"


@pytest.mark.parametrize(
    "name",
    ["chicken stock", "beef broth", "chicken seasoning"],
)
def test_non_equivalent_products_do_not_inherit_meat_prices(name: str) -> None:
    assert _catalogue().resolve(name, "EU")["match_status"] == "unmatched"


def test_upstream_canonical_name_is_used_before_fuzzy_fallback() -> None:
    match = _catalogue().resolve(
        "alas de pollo", "EU", canonical_name="chicken"
    )
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"] == "exact_base:upstream_canonical_name"


@pytest.mark.parametrize(
    ("name", "canonical"),
    [("rice vinegar", "rice"), ("chicken stock", "chicken")],
)
def test_upstream_nutrition_name_cannot_erase_economic_product_context(
    name: str, canonical: str
) -> None:
    assert _catalogue().resolve(name, "EU", canonical_name=canonical)[
        "match_status"
    ] == "unmatched"


def test_explicit_cost_product_id_bypasses_name_matching() -> None:
    match = _catalogue().resolve(
        "alas de pollo", "EU", ingredient_id="base__chicken"
    )
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"] == "exact_cost_product_id"


def test_complete_recipe_cost_includes_a_plain_language_explanation() -> None:
    result = calculate_recipe_cost_profile(
        [
            {"name": "chicken wings", "weight_g": 1000},
            {"name": "ground beef", "weight_grams": 500},
        ],
        servings=3,
        country="EU",
        catalogue=_catalogue(),
    )
    assert result["status"] == "complete"
    assert result["estimated_recipe_cost_total_eur"] == pytest.approx(11.0)
    assert result["estimated_recipe_cost_per_serving_eur"] == pytest.approx(11 / 3)
    assert result["recipe_cost_tier"] is None
    assert result["cost_weight_coverage"] == pytest.approx(1.0)
    assert result["base_fallback_ingredients"] == ["chicken wings"]
    assert "No recipe-level category" in result["explanation"]


def test_recipe_category_uses_fixed_recipe_thresholds_and_all_contributors() -> None:
    calibration = RecipeCostCalibration(
        calibration_version="recipe-test-v1",
        q33_cost_per_serving_eur=2.0,
        q67_cost_per_serving_eur=4.0,
        reference_recipe_count=100,
        minimum_weight_coverage=0.9,
    )
    result = calculate_recipe_cost_profile(
        [
            {"name": "chicken wings", "weight_g": 1000},
            {"name": "ground beef", "weight_g": 500},
        ],
        servings=3,
        country="EU",
        catalogue=_catalogue(),
        calibration=calibration,
    )
    facet = result["cost_facet"]
    assert facet["category"] == "medium"
    assert facet["category_code"] == 2
    assert facet["region"] == "EU"
    assert "confidence" not in facet
    assert [item["ingredient"] for item in facet["contributors"]] == [
        "chicken wings",
        "ground beef",
    ]
    assert facet["contributors"][0]["matched_product"] == "chicken"
    assert facet["contributors"][0]["price_scope"] == "base_product"
    assert facet["contributors"][0]["price_class"] == "higher-cost"
    assert facet["contributors"][0]["cost_contribution_pct"] == pytest.approx(54.5)
    assert "Medium-cost recipe" in facet["explanation"]
    assert "€" not in facet["explanation"]
    assert result["recipe_cost_tier"] == 2


def test_low_coverage_recipe_keeps_category_and_reports_numeric_coverage() -> None:
    calibration = RecipeCostCalibration(
        calibration_version="recipe-test-v1",
        q33_cost_per_serving_eur=2.0,
        q67_cost_per_serving_eur=4.0,
        reference_recipe_count=100,
        minimum_weight_coverage=0.9,
    )
    result = calculate_recipe_cost_profile(
        [
            {"name": "chicken", "weight_g": 500},
            {"name": "vegetable stock", "weight_g": 500},
        ],
        servings=2,
        country="EU",
        catalogue=_catalogue(),
        calibration=calibration,
    )
    facet = result["cost_facet"]
    assert facet["category"] == "low"
    assert facet["status"] == "classified"
    assert facet["priced_weight_coverage"] == pytest.approx(0.5)
    assert facet["priced_ingredient_coverage"] == pytest.approx(0.5)
    assert [item["ingredient"] for item in facet["contributors"]] == ["chicken"]
    assert "confidence" not in facet
    assert "coverage_warning" not in facet


def test_foodon_group_fallback_uses_base_product_median_and_is_transparent() -> None:
    calibration = RecipeCostCalibration(
        calibration_version="recipe-test-v1",
        q33_cost_per_serving_eur=2.0,
        q67_cost_per_serving_eur=4.0,
        reference_recipe_count=100,
        minimum_weight_coverage=0.9,
    )
    result = calculate_recipe_cost_profile(
        [{"name": "generic meat filling", "weight_g": 1000, "cost_group": "meat"}],
        servings=2,
        country="EU",
        catalogue=_catalogue(),
        calibration=calibration,
    )

    assert result["matched_cost_lower_bound_eur"] == pytest.approx(7.5)
    assert result["group_fallback_ingredients"] == ["generic meat filling"]
    contributor = result["cost_facet"]["contributors"][0]
    assert contributor["matched_product"] == "meat group"
    assert contributor["price_scope"] == "foodon_group"


@pytest.mark.parametrize(
    ("ingredient", "group"),
    [("chicken stock", "meat"), ("fish sauce", "fish_seafood")],
)
def test_foodon_group_does_not_price_processed_context_as_solid_food(
    ingredient: str, group: str
) -> None:
    result = calculate_recipe_cost_profile(
        [{"name": ingredient, "weight_g": 500, "cost_group": group}],
        servings=2,
        country="EU",
        catalogue=_catalogue(),
    )

    assert result["matched_ingredient_count"] == 0
    assert result["ingredients"][0]["reason"] == (
        "unsafe_processed_context_for_foodon_group"
    )


def test_partial_recipe_withholds_complete_total() -> None:
    result = calculate_recipe_cost_profile(
        [
            {"name": "chicken", "weight_g": 500},
            {"name": "vegetable stock", "weight_g": 500},
        ],
        servings=2,
        country="EU",
        catalogue=_catalogue(),
    )
    assert result["status"] == "partial"
    assert result["matched_cost_lower_bound_eur"] == pytest.approx(3.0)
    assert result["estimated_recipe_cost_total_eur"] is None
    assert result["estimated_recipe_cost_per_serving_eur"] is None
    assert result["cost_weight_coverage"] == pytest.approx(0.5)
    assert result["unmatched_ingredients"] == ["vegetable stock"]
    assert "complete recipe cost is withheld" in result["explanation"]


def test_missing_weight_is_reported_instead_of_silently_filled() -> None:
    result = calculate_recipe_cost_profile(
        [{"name": "chicken"}],
        servings=2,
        country="EU",
        catalogue=_catalogue(),
    )
    assert result["status"] == "partial"
    assert result["unresolved_weight_ingredients"] == ["chicken"]
    assert result["ingredients"][0]["cost_status"] == "unresolved_weight"


def test_profile_and_batch_adapters_share_the_same_calculator() -> None:
    profile = {
        "recipe_id": "r1",
        "title": "Chicken",
        "region": "IE",
        "serves": 2,
        "ingredients": [{"name": "chicken", "weight_g": 1000}],
    }
    direct = calculate_cost_from_profile(profile, catalogue=_catalogue())
    batch = calculate_recipe_batch([profile], "IE", catalogue=_catalogue())
    assert direct["estimated_recipe_cost_total_eur"] == pytest.approx(7.0)
    assert batch[0]["recipe_id"] == "r1"
    assert batch[0]["estimated_recipe_cost_total_eur"] == pytest.approx(7.0)

    stored_profile = {
        "nutrition_source": "slovenian",
        "profiling_quality": {"serves": 2},
        "nutrition_profiling_details": [
            {"ingredient": "chicken", "weight_g": 1000}
        ],
    }
    stored = calculate_cost_from_profile(stored_profile, catalogue=_catalogue())
    assert stored["country"] == "SI"
    assert stored["estimated_recipe_cost_total_eur"] == pytest.approx(5.5)


@pytest.mark.skipif(
    not (PROCESSED_DIR / "ingredient_prices_classified.csv").exists(),
    reason="Generated pricing outputs are not distributed with source control",
)
def test_actual_catalogue_maps_chicken_wings_to_chicken_base() -> None:
    load_cost_catalogue.cache_clear()
    match = load_cost_catalogue().resolve("chicken wings", "SI")
    assert match["matched_product_id"] == "base__chicken"
    assert match["match_method"].startswith("reviewed_alias")
    assert match["price_scope"] == "base_product"
