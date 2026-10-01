from scripts.analyze_regional_nutrient_drift import ingredients, number, same_weights, serves


def test_comparison_keeps_servings_separate_from_recipe_weight():
    row = {
        'total_nutrients': {'energy_kcal': 800, 'protein_g': 40},
        'total_nutrients_per_serving': {'energy_kcal': 200, 'protein_g': 10},
        'nutrition_profiling_details': [
            {'name': 'Onion', 'weight_g': 100},
            {'ingredient': 'onion', 'weight_g': 50},
        ],
    }
    assert serves(row) == 4
    assert ingredients(row) == {'onion': 150}
    assert same_weights(ingredients(row), {'onion': 150.001})
    assert not same_weights(ingredients(row), {'onion': 75})
    assert not same_weights(ingredients(row), {'carrot': 150})
    assert serves({'total_nutrients': {}, 'total_nutrients_per_serving': {}}) is None
    assert number(None) is None
    assert number('NaN') is None
    assert number(0) == 0
