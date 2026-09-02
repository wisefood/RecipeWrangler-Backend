from __future__ import annotations

import pytest

from recipe_wrangler.pricing.recipe_cost_categories import (
    RecipeCostCategoryConfig,
    build_recipe_cost_calibration,
)


def test_calibration_uses_only_high_coverage_recipes() -> None:
    calibration = build_recipe_cost_calibration(
        [
            {
                "matched_cost_lower_bound_per_serving_eur": 1.0,
                "cost_weight_coverage": 1.0,
                "unresolved_weight_ingredients": [],
            },
            {
                "matched_cost_lower_bound_per_serving_eur": 2.0,
                "cost_weight_coverage": 0.9,
                "unresolved_weight_ingredients": [],
            },
            {
                "matched_cost_lower_bound_per_serving_eur": 5.0,
                "cost_weight_coverage": 0.95,
                "unresolved_weight_ingredients": [],
            },
            {
                "matched_cost_lower_bound_per_serving_eur": 100.0,
                "cost_weight_coverage": 0.74,
                "unresolved_weight_ingredients": [],
            },
        ],
        config=RecipeCostCategoryConfig(min_calibration_recipes=3),
    )
    assert calibration.reference_recipe_count == 3
    assert calibration.q33_cost_per_serving_eur == pytest.approx(1.6666666667)
    assert calibration.q67_cost_per_serving_eur == pytest.approx(3.0)


def test_calibration_rejects_an_insufficient_reference_corpus() -> None:
    with pytest.raises(ValueError, match="Not enough high-coverage"):
        build_recipe_cost_calibration(
            [
                {
                    "matched_cost_lower_bound_per_serving_eur": 2.0,
                    "cost_weight_coverage": 1.0,
                    "unresolved_weight_ingredients": [],
                }
            ],
            config=RecipeCostCategoryConfig(min_calibration_recipes=2),
        )
