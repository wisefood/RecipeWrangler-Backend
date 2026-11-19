"""A candidate must never add an allergen or break a diet the original recipe respected."""
from recipe_wrangler.services.adaptation import nutrition_adaptation as sg


def swap(label, source='x'):
    return {'operation': 'SWAP_FOOD', 'candidate_label': label, 'source_line': source}


def problems(parts, lines):
    return sg.restriction_problems(parts, lines)


def test_gluten_free_recipe_cannot_get_wheat():
    assert problems([swap('Pasta, wholewheat, spaghetti, dried, raw')], ['200 g rice pasta', '1 onion'])


def test_wheat_to_wheat_is_fine():
    assert not problems([swap('Pasta, wholewheat, spaghetti, dried, raw')], ['200 g spaghetti (wheat pasta)'])


def test_vegan_recipe_cannot_get_dairy_or_meat():
    assert problems([swap('Cheese, gouda')], ['100 g tofu', '1 onion'])
    assert problems([swap('Turkey breast, skinless boneless raw')], ['100 g tofu'])


def test_dairy_free_recipe_cannot_get_milk_product():
    assert problems([swap('reduced-fat crème fraîche')], ['100 ml coconut milk'])


def test_extra_vegetable_cannot_add_celery_allergen():
    assert problems([{'operation': 'INCREASE_VEG', 'candidate_label': 'Celery, raw'}], ['200 g pasta', '1 onion'])


def test_celery_already_in_recipe_is_fine():
    assert not problems([{'operation': 'INCREASE_VEG', 'candidate_label': 'Celery, raw'}], ['2 sticks celery'])


def test_pescatarian_recipe_cannot_get_poultry():
    assert problems([swap('Chicken breast')], ['150 g cod fillet', '1 lemon'])


def test_amount_cut_changes_nothing():
    assert not problems([{'operation': 'REDUCE_AMOUNT', 'source_line': '10 g butter'}], ['10 g butter'])


def test_stock_cube_with_unknown_allergens_is_blocked_when_recipe_has_none():
    assert problems([swap('Vegetable stock cube')], ['200 g carrots'])


def test_combined_checks_every_part():
    parts = [{'operation': 'REDUCE_AMOUNT'}, swap('Cheese, gouda')]
    assert problems(parts, ['tofu'])


def test_replaced_food_counts_as_already_present():
    # 'ciabatta' is not a keyword, but the matched food (bread) is wheat, so wheat -> wholemeal wheat is not an addition
    assert not problems([dict(swap('Bread rolls, wholemeal'), matched_label='Bread, white, average')], ['50g ciabatta'])


from recipe_wrangler.services.adaptation import nutrition_adaptation_llm as sgl


def _final(op, new, original):
    candidate = {'changes': [{'position': 1, 'operation': op}]}
    return sgl.final_line_problems(candidate, [{'position': 1, 'text': original}, {'position': 2, 'text': '1 onion'}], [{'position': 1, 'new': new}])


def test_final_lines_block_celery_stock_cube_after_rewrite():
    assert _final('SWAP_FOOD', 'use 1 vegetable stock cube', '1 tsp salt')


def test_final_lines_accept_same_allergen_swap():
    assert not _final('SWAP_FOOD', 'use 200 g wholewheat spaghetti', '200 g spaghetti (wheat pasta)')


def test_final_lines_ignore_amount_cut():
    assert not _final('REDUCE_AMOUNT', 'use 5 g butter', '10 g butter')


def test_legacy_evaluators_use_the_same_gate():
    from recipe_wrangler.services.adaptation import service
    details = [{'ingredient': '100 g tofu'}, {'ingredient': '1 onion'}]
    assert service._restriction_blocked({'name': 'Cheese, gouda'}, {'name': 'tofu'}, details)
    assert not service._restriction_blocked({'name': 'tempeh'}, {'name': 'tofu'}, details)


# ---- user restrictions (allergens and diet sent with the request) ----

def user(parts, lines, allergens=(), diet=None):
    return sg.restriction_problems(parts, lines, allergens, diet)


def test_user_allergen_blocks_even_when_the_recipe_already_has_it():
    # recipe has milk, so the recipe-relative gate passes; the user's milk allergy must still block
    parts = [swap('reduced-fat crème fraîche')]
    assert not user(parts, ['100 ml cream'])
    assert user(parts, ['100 ml cream'], allergens=['milk'])


def test_user_allergen_aliases_are_resolved():
    assert user([swap('reduced-fat crème fraîche')], ['100 ml cream'], allergens=['lactose'])
    assert user([swap('Pasta, wholewheat, spaghetti, dried, raw')], ['200 g spaghetti'], allergens=['gluten'])


def test_user_allergen_does_not_block_other_foods():
    assert not user([swap('Pasta, wholewheat, spaghetti, dried, raw')], ['200 g spaghetti'], allergens=['milk'])


def test_user_diet_blocks_even_when_the_recipe_already_breaks_it():
    parts = [swap('Turkey breast, skinless boneless raw')]
    assert not user(parts, ['300 g beef mince'])
    assert user(parts, ['300 g beef mince'], diet='vegetarian')
    assert user(parts, ['300 g beef mince'], diet='pescatarian')


def test_user_vegan_blocks_dairy_swap():
    assert user([swap('Cheese, gouda')], ['100 g cheddar'], diet='vegan')
    assert not user([swap('Cheese, gouda')], ['100 g cheddar'], diet='vegetarian')


def test_user_restrictions_ignore_amount_cuts():
    assert not user([{'operation': 'REDUCE_AMOUNT', 'source_line': '10 g butter'}], ['10 g butter'], allergens=['milk'], diet='vegan')


def test_recipe_lines_that_break_the_user_restriction_are_listed():
    lines = ['200 g spaghetti', '50 g butter', '1 onion']
    assert sg.user_restriction_problems(lines, ['milk'], None) == ['50 g butter: contains allergen milk (user restriction)']
    assert sg.user_restriction_problems(lines, [], 'vegan')
    assert sg.user_restriction_problems(lines, [], None) == []


def test_route_passes_the_restrictions_and_defaults_to_none(monkeypatch):
    from recipe_wrangler.services.adaptation import router, schemas
    seen = {}
    monkeypatch.setattr(sg, 'adapt_active_recipe', lambda rid, src, allergens=(), diet=None: seen.update(a=list(allergens), d=diet) or {})
    router.nutrition_adaptation(schemas.SmallGainRequest(region='IE'), 'r1')
    assert seen == {'a': [], 'd': None}
    router.nutrition_adaptation(schemas.SmallGainRequest(region='IE', allergens=['milk'], diet='vegan'), 'r1')
    assert seen == {'a': ['milk'], 'd': 'vegan'}
