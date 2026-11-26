"""Oil listed for frying stays in the pan; only the absorbed share is eaten (26 Nov, calorie outliers)."""
from recipe_wrangler.matching.quantities import recipe_inputs


def weights(names, grams, originals):
    return [q['weight_g'] for q in recipe_inputs(names, grams, originals)['quantities']]


def test_frying_oil_counts_ten_percent_of_the_fried_food():
    # Styrian Strauben: 500 ml oil for frying, about 1.2 kg of batter.
    w = weights(['flour', 'milk', 'eggs', 'oil'], [500, 500, 200, 460],
                ['500 g all-purpose flour', '500 ml milk', '4 eggs', '500 ml of oil for frying'])
    assert w[3] == 0.10 * sum(w[:3])  # 10% of the ~1.2 kg batter, not 460 g


def test_cooking_oil_without_frying_is_kept():
    w = weights(['flour', 'olive oil'], [500, 50], ['500 g flour', '50 g olive oil'])
    assert w[1] == 50


def test_absorbed_share_never_exceeds_the_oil_listed():
    w = weights(['potatoes', 'oil'], [1000, 30], ['1 kg potatoes', '2 tablespoons oil for shallow frying'])
    assert w[1] == 30


def test_oil_used_in_the_dish_plus_extra_for_frying_is_kept():
    w = weights(['flour', 'coconut oil'], [100, 28], ['100 g flour', '2 tablespoons coconut oil, melted, plus 1 teaspoon extra for frying'])
    assert w[1] == 28
