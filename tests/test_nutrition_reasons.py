from recipe_wrangler.services.adaptation.nutrition_reasons import WHY, reason_code


def res(grade, tally='', note=None):
    return {'baseline': {'grade': 'Nutriscore_' + grade}, 'reason': 'No candidate passed. Rejection tally: ' + tally, 'amount_recalculation_note': note}


def test_every_reason_has_a_sentence_and_the_rules_pick_the_right_one():
    ex = lambda identity: {'excess': [{'points': 4, 'share': 0.6, 'is_dish_identity': identity}]}
    assert reason_code(res('A'), {'excess': []}) == 'already_healthy'
    assert reason_code(res('B'), ex(True)) == 'main_food_defines_the_dish'
    assert reason_code(res('C', 'restriction x2'), ex(False)) == 'alternatives_conflict_with_diet_or_allergen'
    assert reason_code(res('C', 'no_meaningful_improvement x2'), ex(False)) == 'gain_too_small'
    assert reason_code(res('C', 'no candidates retrieved'), ex(False)) == 'no_healthier_option'
    assert reason_code(res('C', note='recalculation_unavailable:x'), ex(False)) == 'not_scored'
    assert {reason_code(res('A'), {'excess': []}), 'no_healthier_option'} <= set(WHY)
