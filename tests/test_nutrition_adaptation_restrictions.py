"""A candidate must never add an allergen or break a diet the original recipe respected."""
from recipe_wrangler.services.adaptation import nutrition_adaptation as sg


import pytest


@pytest.fixture(autouse=True)
def no_graph(monkeypatch):
    monkeypatch.setattr(sg, 'USE_TAGS', False)  # the tag tests below switch it on with a fake graph


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


# ---- stored ingredient tags (HAS_ALLERGEN, SUITABILITY_FOR) as an extra blocker for the user's restrictions ----

def tags(monkeypatch, table):
    monkeypatch.setattr(sg, 'ingredient_tags', lambda keys: {k: table[k] for k in keys if k in table})


def test_tag_keys_prefer_the_specific_name():
    assert sg.tag_keys('Cheese, Mozzarella, fresh')[0] == 'cheese mozzarella fresh'
    assert 'coconut milk' in sg.tag_keys('Coconut milk, light, canned') and '' not in sg.tag_keys('Oil')


def test_stored_allergen_tag_blocks_a_food_the_keywords_miss(monkeypatch):
    tags(monkeypatch, {'zorbo': {'allergens': ['milk'], 'suitability': {}}})
    parts = [swap('Zorbo')]
    assert not user(parts, ['100 g tofu'], allergens=['milk'])  # keywords do not know quark
    monkeypatch.setattr(sg, 'USE_TAGS', True)
    assert any('stored tag' in r for r in user(parts, ['100 g tofu'], allergens=['milk']))
    assert not user(parts, ['100 g tofu'], allergens=['gluten'])


def test_stored_diet_tag_blocks_not_suitable_only(monkeypatch):
    monkeypatch.setattr(sg, 'USE_TAGS', True)
    tags(monkeypatch, {'zorbo': {'allergens': [], 'suitability': {'vegan': 'not_suitable', 'vegetarian': 'unknown'}}})
    assert user([swap('Zorbo')], ['100 g tofu'], diet='vegan')
    assert not user([swap('Zorbo')], ['100 g tofu'], diet='vegetarian')  # unknown falls back to the keyword check


def test_tags_are_not_read_without_user_restrictions(monkeypatch):
    monkeypatch.setattr(sg, 'USE_TAGS', True)
    monkeypatch.setattr(sg, 'ingredient_tags', lambda keys: (_ for _ in ()).throw(AssertionError('graph read')))
    assert not user([swap('Zorbo')], ['100 g tofu'])


def test_graph_failure_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(sg, 'USE_TAGS', True)
    monkeypatch.setattr(sg, 'ingredient_tags', lambda keys: (_ for _ in ()).throw(RuntimeError('neo4j down')))
    assert user([swap('Cheese, gouda')], ['100 g tofu'], diet='vegan')


def stored(lines, parts, status='adapted'):
    return {'status': status, 'is_adapted': status == 'adapted', 'original': {'lines': lines}, 'adapted': {'lines': lines}, 'swap': parts, 'benefit': {'tip': 't'}}


def test_stored_row_passes_when_nothing_conflicts():
    row = stored(['200 g pasta'], [swap('Pasta, wholewheat, dried, raw')])
    assert sg.restrict_stored_row(row, ['nuts'], 'vegetarian')['status'] == 'adapted'


def test_stored_row_blocked_when_recipe_has_the_users_allergen():
    out = sg.restrict_stored_row(stored(['200 g dried pasta'], [swap('Pasta, wholewheat, dried, raw')]), ['gluten'])
    assert out['status'] == 'not_suitable_for_user' and out['reason_code'] == 'recipe_breaks_user_restriction' and out['offending']


def test_stored_row_blocked_when_the_swap_adds_the_users_allergen():
    out = sg.restrict_stored_row(stored(['1 tbsp oil'], [swap('Almond butter')]), ['nuts'])
    assert out['status'] == 'no_suggestion' and out['reason_code'] == 'alternatives_add_allergen' and out['swap'] is None


def test_stored_no_suggestion_row_is_returned_with_the_restrictions():
    out = sg.restrict_stored_row({'status': 'no_suggestion', 'reason_code': 'already_healthy'}, ['milk'])
    assert out['reason_code'] == 'already_healthy' and out['applied_restrictions']['allergens'] == ['milk']


def live_result(outcome, **extra):
    return dict({'recipe_id': 'r1', 'title': 'T', 'outcome': outcome}, **extra)


def test_live_row_adapted_has_the_stored_row_shape():
    changes = [{'position': 0, 'source_line': '200 g pasta', 'candidate_label': 'Pasta, wholewheat'}]
    result = live_result('adapted', adaptation={'changes': changes, 'gates': {'improvements': [{'metric': 'fibre_g'}]}, 'grade_before': 'A', 'grade_after': 'A',
                                                'before_per_serving': {'fibre_g': 3.0}, 'after_per_serving': {'fibre_g': 12.0}},
                         card={'ingredients': [{'old': '200 g pasta', 'new': '200 g wholewheat pasta'}, {'old': 'salt', 'new': ''}], 'notes': [], 'instructions': ['1. Boil.']})
    row = sg.live_row(result, 'IE')
    assert row['status'] == 'adapted' and row['is_adapted'] and row['source'] == 'live'
    assert row['adapted']['lines'] == ['200 g wholewheat pasta', 'salt'] and row['adapted']['changed_positions'] == [0]
    assert row['benefit']['metric']['change'] == 9.0 and row['original']['steps'] == ['Boil.']


def test_live_row_other_outcomes():
    assert sg.live_row(live_result('no_sensible_adaptation'), 'IE')['status'] == 'no_suggestion'
    assert sg.live_row(live_result('failed', reason='no_active_profile'), 'IE')['status'] == 'not_scored'
    blocked = sg.live_row(live_result('not_suitable_for_user', reason='recipe_breaks_user_restriction', offending=['x']), 'IE')
    assert blocked['status'] == 'not_suitable_for_user' and blocked['offending'] == ['x']
    enriched = sg.live_row(live_result('enriched', enrichment={'addition': 'lentils', 'grams_per_serving': 40},
                                       card={'serve_with': 'Serve with lentils.', 'benefit': '+2 g fibre', 'note': 'n', 'ingredients': [{'old': 'rice', 'new': ''}], 'instructions': ['Cook.']}), 'IE')
    assert enriched['is_adapted'] and enriched['adapted']['lines'] == ['rice', 'Serve with lentils.'] and enriched['original']['lines'] == ['rice']


def test_restricted_request_never_falls_back_to_the_stored_row_when_the_live_engine_fails(monkeypatch):
    from recipe_wrangler.repositories import recipe_adaptations
    from recipe_wrangler.services.adaptation import adaptation_row as ar
    stored_row = stored(['200 g pasta'], [swap('Pasta, wholewheat, dried, raw')])
    monkeypatch.setattr(recipe_adaptations, 'get_adaptation', lambda *a: dict(stored_row))
    monkeypatch.setitem(ar.MODES, 'sustainability', (lambda *a: {'status': 'failed', 'reason_code': 'x'}, True, {}))
    assert ar.adaptation_row('r1', 'sustainability', 'IE', ['nuts'])['status'] == 'not_scored'
    monkeypatch.setattr(recipe_adaptations, 'get_adaptation', lambda *a: None)
    assert ar.adaptation_row('r1', 'sustainability', 'IE', ['nuts'])['status'] == 'not_scored'


def test_stored_row_with_a_single_swap_object_is_checked_too():
    row = stored(['1 x tin tuna'], swap('Sardines, canned in brine, drained'))
    assert sg.restrict_stored_row(row, ['fish'])['status'] == 'not_suitable_for_user'
    assert sg.restrict_stored_row(stored(['pasta'], swap('Almond butter')), ['nuts'])['status'] == 'no_suggestion'
