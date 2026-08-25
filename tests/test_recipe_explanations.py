from recipe_wrangler.api.routers.recipes import (
    _calculation_disclaimer,
    _extract_profiling_quality,
    _nutri_score_explanation,
    _sustainability_explanation,
)


def test_nutri_score_e_explains_drivers_and_improve_route() -> None:
    explanation = _nutri_score_explanation(
        "Nutriscore_E",
        {
            "negative_points": {
                "items": {
                    "sugar": {"points": 9, "value_per_100g": 40, "unit": "g"},
                    "sodium": {"points": 2, "value_per_100g": 300, "unit": "mg"},
                }
            },
            "positive_points": {
                "items": {
                    "fiber": {"points": 1, "value_per_100g": 1.5, "unit": "g"}
                }
            },
        },
        "recipe-1",
    )

    assert explanation is not None
    assert explanation["grade"] == "E"
    assert explanation["main_negative_drivers"][0]["factor"] == "sugar"
    assert explanation["improve_endpoint"].endswith("/recipe-1/adapt/suggestions")


def test_low_confidence_profile_requires_disclaimer() -> None:
    quality = {
        "serves_source": "estimated",
        "weights_capped": True,
        "nutrition_low_coverage": True,
    }
    disclaimer = _calculation_disclaimer(
        quality, [{"match_confidence": "weak"}]
    )
    assert disclaimer["required"] is True
    assert set(disclaimer["reasons"]) == {
        "servings_estimated",
        "ingredient_weights_sanity_adjusted",
        "low_nutrition_coverage",
        "weak_ingredient_matches",
    }


def test_quality_can_be_read_from_compact_column_or_debug() -> None:
    assert _extract_profiling_quality({"profiling_quality": {"nutrition_coverage": 1}}) == {
        "nutrition_coverage": 1
    }
    assert _extract_profiling_quality(
        {"nutrition_profiling_debug": {"profiling": {"quality": {"weights_capped": False}}}}
    ) == {"weights_capped": False}


def test_sustainability_explanation_ranks_contributors() -> None:
    explanation = _sustainability_explanation(
        1.2,
        [
            {"name": "beans", "contribution": 0.1},
            {"name": "beef", "contribution": 1.0},
        ],
        {"sustainability_coverage": 0.9},
    )
    assert explanation is not None
    assert explanation["top_contributors"][0]["ingredient"] == "beef"
    assert explanation["coverage"] == 0.9
