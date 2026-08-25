"""Regression tests for the USDA portion-weight-only boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from recipe_wrangler.schemas.models import RecipeProfileRequest
from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_vector
from recipe_wrangler.utils.fruit_vegetable_content import (
    fruits_veg_legumes_percent,
)


def test_us_region_is_not_a_nutrition_api_option() -> None:
    with pytest.raises(ValidationError):
        RecipeProfileRequest(raw_recipe="1 apple", region="US")


@pytest.mark.parametrize("region", ["IE", "HU", "EU", "SI"])
def test_supported_regional_nutrition_options(region: str) -> None:
    request = RecipeProfileRequest(raw_recipe="1 apple", region=region)
    assert request.region == region


def test_calculator_rejects_usda_before_any_lookup() -> None:
    with pytest.raises(ValueError, match="Unsupported nutrition source 'usda'"):
        nutritional_tool_vector.invoke(
            {
                "title": "Test",
                "ingredient_names": ["apple"],
                "weights": [100.0],
                "source": "usda",
            }
        )


def test_fvln_percentage_uses_foodon_groups_and_names_without_usda_ids() -> None:
    ingredients = [
        {
            "name": "unknown ontology ingredient",
            "weight_grams": 100,
            "ingredient_class_ancestors": ["FOODON_00001057"],
        },
        {"name": "red lentils", "weight_grams": 50},
        {"name": "chicken breast", "weight_grams": 50},
    ]
    assert fruits_veg_legumes_percent(ingredients) == pytest.approx(75.0)


def test_usda_id_alone_does_not_classify_fvln() -> None:
    ingredients = [
        {"name": "unclassified food", "weight_grams": 100, "usda_id": "09003"},
        {"name": "chicken", "weight_grams": 100},
    ]
    assert fruits_veg_legumes_percent(ingredients) == 0.0
