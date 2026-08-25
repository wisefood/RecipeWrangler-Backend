from recipe_wrangler.utils.nutrition_claims import (
    compute_nutrition_claim_tags,
    infer_physical_form,
)


def test_all_claim_thresholds_and_nutriscore_a():
    tags = compute_nutrition_claim_tags(
        {
            "energy_kcal": 40,
            "fat_g": 3,
            "fibre_g": 6,
            "protein_g": 2,
        },
        [{"weight_g": 100}],
        {"nutri_score": "Nutriscore_A"},
    )
    assert tags == [
        "low_calorie",
        "low_fat",
        "high_fibre",
        "high_protein",
        "healthy_and_nutritious",
    ]


def test_missing_nutrients_do_not_become_zero_value_claims():
    assert compute_nutrition_claim_tags({}, [{"weight_g": 100}]) == []


def test_per_100g_claims_require_positive_recipe_weight():
    tags = compute_nutrition_claim_tags(
        {"energy_kcal": 20, "fat_g": 1, "fibre_g": 10, "protein_g": 1},
        [],
    )
    assert "low_calorie" not in tags
    assert "low_fat" not in tags
    assert "high_fibre" not in tags
    assert "high_protein" in tags


def test_eu_liquid_thresholds_are_stricter_than_solid_thresholds():
    nutrients = {"energy_kcal": 30, "fat_g": 2}
    details = [{"weight_g": 100}]
    assert {"low_calorie", "low_fat"} <= set(
        compute_nutrition_claim_tags(nutrients, details, physical_form="solid")
    )
    liquid = compute_nutrition_claim_tags(
        nutrients, details, physical_form="liquid"
    )
    assert "low_calorie" not in liquid
    assert "low_fat" not in liquid


def test_beverage_physical_form_can_come_from_facet_or_clear_title():
    assert infer_physical_form("Berry smoothie") == "liquid"
    assert infer_physical_form("House recipe", ["beverages"]) == "liquid"
    assert infer_physical_form("Coffee cake") == "solid"
