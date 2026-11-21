"""Reason code and user-facing sentence when a recipe gets no nutrition suggestion (same idea as sustainability_reasons: stored as reason_code in recipe_adaptations)."""

WHY = {
    'already_healthy': 'The recipe already scores A and no swap, cut or addition improves it by a visible amount without making another nutrient worse.',
    'main_food_defines_the_dish': 'The nutrient that costs the most points comes from what the dish is made of (for example the bread, scones or banana bread itself), so changing it would change the dish.',
    'no_healthier_option': 'There is no healthier version of the main contributor that fits the dish and passes our culinary-role, evidence and nutrition checks.',
    'gain_too_small': 'Healthier options exist but the gain per serving is too small to be worth changing the recipe.',
    'alternatives_conflict_with_diet_or_allergen': 'Healthier options would add an allergen or break a diet the recipe respects.',
    'not_scored': 'The saved nutrition profile of this recipe cannot be reproduced reliably, so no change is suggested.',
}


def reason_code(result: dict, diagnosis: dict) -> str:
    """Why `result` (outcome no_sensible_adaptation) has no suggestion. diagnosis comes from diagnosis.diagnose on the saved baseline."""
    if (result.get('amount_recalculation_note') or '').startswith('recalculation_unavailable'):
        return 'not_scored'
    tally = (result.get('reason') or '').split('Rejection tally:')[-1]
    if result['baseline']['grade'][-1] == 'A':
        return 'already_healthy'
    excess = [e for e in diagnosis.get('excess', []) if e['points'] > 0]
    top = max(excess, key=lambda e: e['points'] * e['share']) if excess else None
    if top and top['is_dish_identity'] and top['points'] >= 3:
        return 'main_food_defines_the_dish'
    if 'restriction' in tally:
        return 'alternatives_conflict_with_diet_or_allergen'
    if any(k in tally for k in ('no_meaningful_improvement', 'gain_below', 'fibre_gain_below')):
        return 'gain_too_small'
    return 'no_healthier_option'
