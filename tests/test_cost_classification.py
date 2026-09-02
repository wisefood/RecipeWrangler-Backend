from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from recipe_wrangler.pricing.constants import PROCESSED_DIR
from recipe_wrangler.pricing.cost_classification import (
    assign_global_cost_tier,
    calculate_recipe_cost,
    compute_category_positions,
    compute_global_thresholds,
    compute_pairwise_cost_saving,
    compute_tier_boundary_margin,
    describe_detail_to_base,
    empirical_base_percentiles,
    percentiles_against_base_reference,
)


def _category(prices: list[float | None], name: str = "test") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ingredient_id": [f"item_{index}" for index in range(len(prices))],
            "ingredient_name": [f"item {index}" for index in range(len(prices))],
            "food_group": name,
            "estimated_eur_per_kg": prices,
        }
    )


def test_global_thresholds_use_explicit_linear_tertiles() -> None:
    frame = _category([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert compute_global_thresholds(frame) == pytest.approx(
        (
            np.quantile(frame["estimated_eur_per_kg"], 1 / 3, method="linear"),
            np.quantile(frame["estimated_eur_per_kg"], 2 / 3, method="linear"),
        )
    )


def test_detail_rows_cannot_change_base_calibration() -> None:
    bases = _category([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    before = compute_global_thresholds(bases)
    details = pd.Series([0.01] * 100 + [1000.0] * 100)
    tiers = assign_global_cost_tier(details, before)
    assert compute_global_thresholds(bases) == before
    assert tiers.value_counts().to_dict() == {"€": 100, "€€€": 100}


def test_empirical_percentiles_use_average_rank_for_ties() -> None:
    result = empirical_base_percentiles(pd.Series([1.0, 2.0, 2.0, 4.0]))
    assert result.tolist() == pytest.approx([0.125, 0.5, 0.5, 0.875])


def test_missing_prices_are_not_ranked_or_classified() -> None:
    frame = _category([1.0, None, 3.0, 4.0])
    positions = compute_category_positions(frame)
    assert positions["category_size"].unique().tolist() == [3]
    assert pd.isna(positions.loc[1, "within_category_rank"])
    tiers = assign_global_cost_tier(frame["estimated_eur_per_kg"], (2.0, 3.0))
    assert pd.isna(tiers.iloc[1])


def test_single_member_category_is_unavailable() -> None:
    result = compute_category_positions(_category([2.0])).iloc[0]
    assert result["within_category_resolution"] == "unavailable"
    assert pd.isna(result["within_category_percentile"])
    assert result["within_category_position"] is None


def test_two_member_category_has_percentiles_but_no_bands() -> None:
    result = compute_category_positions(_category([1.0, 2.0]))
    assert result["within_category_percentile"].tolist() == pytest.approx([0.25, 0.75])
    assert result["within_category_resolution"].eq("insufficient_for_band").all()
    assert result["within_category_position"].isna().all()


def test_four_member_category_has_coarse_positions() -> None:
    result = compute_category_positions(_category([4.0, 6.0, 9.0, 11.0]))
    assert result["within_category_percentile"].tolist() == pytest.approx(
        [0.125, 0.375, 0.625, 0.875]
    )
    assert result["within_category_position"].tolist() == [
        "Low",
        "Middle",
        "Middle",
        "High",
    ]
    assert result["within_category_resolution"].eq("coarse").all()


def test_seven_member_category_has_normal_resolution() -> None:
    result = compute_category_positions(_category(list(range(1, 8))))
    assert result["within_category_resolution"].eq("normal").all()
    assert result.iloc[0]["within_category_position"] == "Low"
    assert result.iloc[-1]["within_category_position"] == "High"


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.79, "substantially_lower"),
        (0.80, "lower"),
        (0.90, "typical"),
        (1.10, "typical"),
        (1.11, "higher"),
        (1.25, "higher"),
        (1.26, "substantially_higher"),
    ],
)
def test_detail_to_base_relationship_boundaries(
    ratio: float, expected: str
) -> None:
    assert describe_detail_to_base(ratio) == expected


def test_detail_percentiles_use_frozen_base_reference() -> None:
    result = percentiles_against_base_reference(
        pd.Series([0.5, 2.0, 5.0]), pd.Series([1.0, 2.0, 3.0, 4.0])
    )
    assert result.tolist() == pytest.approx([0.0, 0.375, 1.0])


def test_boundary_margin_is_zero_at_threshold() -> None:
    result = compute_tier_boundary_margin(pd.Series([2.0, 3.0, 6.0]), (2.0, 6.0))
    assert result.loc[0, "tier_boundary_margin_pct"] == pytest.approx(0.0)
    assert result.loc[2, "tier_boundary_margin_pct"] == pytest.approx(0.0)
    assert result.loc[1, "tier_boundary_margin_pct"] == pytest.approx(50.0)


def test_pairwise_savings_are_directional_and_explainable() -> None:
    result = compute_pairwise_cost_saving(10.0, 6.0)
    assert result["relative_saving"] == pytest.approx(0.4)
    assert result["relative_saving_pct"] == pytest.approx(40.0)
    assert result["log_price_ratio"] == pytest.approx(np.log(10 / 6))
    assert compute_pairwise_cost_saving(6.0, 10.0)["relative_saving"] < 0


def test_recipe_cost_is_quantity_weighted_without_assigning_a_tier() -> None:
    ingredients = pd.DataFrame(
        {
            "ingredient_quantity_kg": [0.5, 0.2],
            "ingredient_price_eur_kg": [8.0, 5.0],
        }
    )
    result = calculate_recipe_cost(ingredients, servings=2)
    assert result == pytest.approx(
        {"recipe_cost_total_eur": 5.0, "recipe_cost_per_serving_eur": 2.5}
    )
    assert "cost_tier" not in result


@pytest.mark.skipif(
    not (PROCESSED_DIR / "ingredient_prices_classified.csv").exists(),
    reason="Generated pricing outputs are not distributed with source control",
)
def test_generated_catalogue_has_expected_base_detail_structure() -> None:
    classified = pd.read_csv(PROCESSED_DIR / "ingredient_prices_classified.csv")
    metadata = pd.read_json(
        PROCESSED_DIR / "cost_classification_metadata.json", typ="series"
    )
    bases = classified[classified["product_level"] == "base"]
    details = classified[classified["product_level"] == "detail"]
    assert len(bases) == 76
    assert len(details) == 246
    assert details["parent_base_product_id"].notna().all()
    assert np.allclose(
        classified["price_index_ie"],
        classified["price_ie_eur_kg"]
        / classified["eu_reference_price_eur_kg"],
    )
    assert np.allclose(
        classified["price_index_hu"],
        classified["price_hu_eur_kg"]
        / classified["eu_reference_price_eur_kg"],
    )
    assert np.allclose(
        classified["price_index_si"],
        classified["price_si_eur_kg"]
        / classified["eu_reference_price_eur_kg"],
    )
    assert metadata["number_of_base_products_used"] == 76
    assert metadata["global_threshold_eur_kg_low_mid"] == pytest.approx(1.1820652174)
    assert metadata["global_threshold_eur_kg_mid_high"] == pytest.approx(3.7792105263)
    assert bases["global_cost_tier"].value_counts().to_dict() == {
        "€€€": 26,
        "€": 25,
        "€€": 25,
    }


@pytest.mark.skipif(
    not (PROCESSED_DIR / "ingredient_prices_classified.csv").exists(),
    reason="Generated pricing outputs are not distributed with source control",
)
def test_generated_meat_comparisons_match_reference_prices() -> None:
    frame = pd.read_csv(PROCESSED_DIR / "ingredient_prices_classified.csv")
    meat = frame[
        (frame["product_level"] == "base") & (frame["food_category"] == "meat")
    ].set_index("canonical_name")
    assert meat.loc["pork", "within_category_position"] == "Low"
    assert meat.loc["chicken", "within_category_position"] == "Middle"
    assert meat.loc["beef", "within_category_position"] == "Middle"
    assert meat.loc["lamb", "within_category_position"] == "High"
    beef_to_chicken = compute_pairwise_cost_saving(
        meat.loc["beef", "eu_reference_price_eur_kg"],
        meat.loc["chicken", "eu_reference_price_eur_kg"],
    )
    assert beef_to_chicken["relative_saving"] == pytest.approx(0.2559, abs=0.001)
    lamb_to_chicken = compute_pairwise_cost_saving(
        meat.loc["lamb", "eu_reference_price_eur_kg"],
        meat.loc["chicken", "eu_reference_price_eur_kg"],
    )
    beef_to_pork = compute_pairwise_cost_saving(
        meat.loc["beef", "eu_reference_price_eur_kg"],
        meat.loc["pork", "eu_reference_price_eur_kg"],
    )
    assert lamb_to_chicken["relative_saving"] == pytest.approx(0.3737, abs=0.001)
    assert beef_to_pork["relative_saving"] == pytest.approx(0.5324, abs=0.001)


def test_threshold_calibration_is_unchanged_by_many_apple_details() -> None:
    bases = _category([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    threshold = compute_global_thresholds(bases)
    apple_details = pd.Series(np.linspace(0.1, 20.0, 1000))
    percentiles_against_base_reference(
        apple_details, bases["estimated_eur_per_kg"]
    )
    assert compute_global_thresholds(bases) == threshold
