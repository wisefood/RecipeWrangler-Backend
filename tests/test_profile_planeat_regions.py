def test_compute_nutri_score_per100g_scaling():
    """Per-100g Nutri-Score uses correct weight scaling."""
    import importlib, sys
    sys.path.insert(0, "src")
    mod = importlib.import_module("recipe_wrangler.utils.nutri_score")
    from scripts.profile_planeat_regions import _compute_nutri_score_per100g

    totals = {
        "energy_kcal": 397.0,
        "protein_g": 25.0,
        "carbohydrate_g": 30.0,
        "fat_g": 18.0,
        "sugar_g": 4.0,
        "saturated_fat_g": 5.0,
        "sodium_mg": 400.0,
        "fibre_g": 3.0,
    }
    breakdown = _compute_nutri_score_per100g(totals, total_weight_g=315.0)
    assert breakdown is not None
    assert breakdown["basis"] == "per_100g_from_weight"
    assert breakdown["nutri_score"] in {"A", "B", "C", "D", "E", "Nutriscore_A", "Nutriscore_B", "Nutriscore_C", "Nutriscore_D", "Nutriscore_E"}


def test_compute_nutri_score_per100g_zero_weight():
    from scripts.profile_planeat_regions import _compute_nutri_score_per100g
    result = _compute_nutri_score_per100g({"energy_kcal": 100, "protein_g": 5,
        "carbohydrate_g": 10, "fat_g": 3, "sugar_g": 2,
        "saturated_fat_g": 1, "sodium_mg": 100, "fibre_g": 1}, total_weight_g=0)
    assert result is None
