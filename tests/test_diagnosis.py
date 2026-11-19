from recipe_wrangler.services.adaptation.diagnosis import diagnose


def breakdown(neg, pos, excluded=False):
    names = dict(energy='energy', sugar='sugar', saturated_fats='saturated_fats', sodium='sodium')
    return {'negative_points': {'total': sum(neg.values()), 'items': {k: {'points': neg.get(k, 0), 'max': 10} for k in names}},
            'positive_points': {'protein_excluded_by_rule': excluded, 'items': {k: {'points': pos.get(k, 0), 'max': 5} for k in ('fiber', 'protein', 'fruit_percentage')}}}


DETAILS = [{'position': 0, 'name': 'cheddar cheese', 'saturated_fat_g': 9.0, 'energy_kcal': 200, 'sugar_g': 0, 'sodium_mg': 300},
           {'position': 1, 'name': 'tomato', 'saturated_fat_g': 0.1, 'energy_kcal': 20, 'sugar_g': 3, 'sodium_mg': 5}]
LINES = {0: '50g cheddar cheese', 1: '2 tomatoes'}


def test_fatty_cheese_is_reduced_first_and_flagged_as_identity():
    d = diagnose(breakdown({'saturated_fats': 6, 'energy': 1}, {'fiber': 1, 'protein': 3}), DETAILS, LINES, 'Cheddar cheese toastie')
    assert d['strategy'] == 'both' and d['excess'][0]['nutrient'] == 'saturated_fats'
    assert d['excess'][0]['main_source'] == '50g cheddar cheese' and d['excess'][0]['is_dish_identity']


def test_light_recipe_that_lacks_fibre_and_fruit_veg_is_enriched():
    d = diagnose(breakdown({'energy': 1}, {'fiber': 0, 'protein': 4, 'fruit_percentage': 0}), DETAILS, LINES, 'Toast')
    assert d['strategy'] == 'enrich' and [g['component'] for g in d['gaps']] == ['fibre', 'fruit_veg']


def test_protein_gap_is_ignored_when_the_protein_rule_excludes_it():
    d = diagnose(breakdown({'saturated_fats': 8, 'energy': 4}, {'fiber': 5, 'protein': 0, 'fruit_percentage': 5}, excluded=True), DETAILS, LINES, 'x')
    assert d['strategy'] == 'reduce' and not d['gaps']


def test_nothing_to_reduce_or_add():
    assert diagnose(breakdown({}, {'fiber': 5, 'protein': 5, 'fruit_percentage': 5}), DETAILS, LINES, 'x')['strategy'] == 'none'
