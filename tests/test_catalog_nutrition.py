from __future__ import annotations

from recipe_wrangler.catalog.nutrition import apply_profiles, profile_summary


def test_cost_facet_is_lifted_to_recipe_and_internal_cost_is_not_projected() -> None:
    row = {
        "recipe_id": "r-1",
        "nutrition_source": "eu",
        "source": "user",
        "nutri_score": {},
        "total_sustainability_per_serving": None,
        "pipeline_version": "test",
        "computed_at": None,
        "nutrition_profiling_debug": {
            "cost_profile": {
                "estimated_recipe_cost_per_serving_eur": 2.75,
                "cost_facet": [{
                    "region": "EU",
                    "category": "medium",
                    "category_code": 2,
                    "status": "classified",
                    "priced_weight_coverage": 0.8,
                    "priced_ingredient_coverage": 0.5,
                    "priced_ingredient_count": 1,
                    "ingredient_count": 2,
                    "contributors": [
                        {
                            "ingredient": "chicken wings",
                            "matched_product": "chicken",
                            "price_scope": "base_product",
                            "price_class": "medium-cost",
                            "cost_contribution_pct": 100.0,
                            "ingredient_cost_eur": 3.0,
                        }
                    ],
                    "explanation": "Medium-cost recipe.",
                    "confidence": "obsolete",
                }],
            }
        },
    }
    profile = profile_summary(row, nutri_label=lambda value: value)
    doc: dict = {}

    apply_profiles(doc, [profile])

    assert "_cost" not in doc["profiles"][0]
    assert doc["cost"][0]["region"] == "EU"
    assert doc["cost"][0]["category"] == "medium"
    assert doc["cost"][0]["contributors"] == [
        {
            "ingredient": "chicken wings",
            "matched_product": "chicken",
            "price_scope": "base_product",
            "price_class": "medium-cost",
            "cost_contribution_pct": 100.0,
        }
    ]
    assert "confidence" not in doc["cost"][0]
    assert "estimated_recipe_cost_per_serving_eur" not in doc["cost"][0]
    assert "ingredient_cost_eur" not in doc["cost"][0]["contributors"][0]
