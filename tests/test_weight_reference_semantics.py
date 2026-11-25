"""Regressions for production-reference selection, independent of Fineli labels."""
import json
from unittest.mock import patch

import pytest

from recipe_wrangler.tools import ingredient_weight_tool as tool
from recipe_wrangler.utils import weigh_calculation_usda_ as weights


@pytest.mark.parametrize('name,canonical', [
    ('almond', 'Oil, almond'),
    ('sugar', 'Syrups, sugar'),
    ('milk powder', 'Milk, fluid'),
])
def test_retrieved_liquid_derivative_cannot_supply_dry_food_density(name, canonical):
    assert tool._liquid_density_for_name(name, canonical=canonical) is None


def test_explicit_liquids_still_get_their_density():
    assert tool._liquid_density_for_name('almond oil') == (0.92, 'oil')
    assert tool._liquid_density_for_name('sugar syrup') == (1.37, 'syrup')
    assert tool._liquid_density_for_name('almond milk') == (1.03, 'milk')


@pytest.mark.parametrize('unit', ['egg', 'whole', 'piece', 'medium'])
def test_raw_quail_egg_uses_existing_usda_species_reference(unit):
    grams, label = tool._common_unit_reference_grams('egg, quail, raw', unit)
    assert grams == 9
    assert '01140' in label


@pytest.mark.parametrize('name,unit', [
    ('quail egg white', 'egg_white'), ('duck egg', 'medium'),
    ('ostrich egg', 'whole'), ('quail egg', 'small'), ('quail egg', 'large'),
    ('boiled quail egg', 'medium'),
])
def test_unknown_species_part_or_size_does_not_inherit_chicken_egg(name, unit):
    assert tool._common_unit_reference_grams(name, unit) is None


def test_link_selection_rejects_unqualified_egg_for_explicit_species():
    assert tool._usda_link_mismatch_reason('quail egg', {'canonical': 'Egg, whole, raw'}) == 'egg_species_mismatch'
    assert tool._usda_link_mismatch_reason('quail egg', {'canonical': 'Egg, quail, whole, raw'}) is None


def test_name_lookup_matches_plural_and_reordered_names_without_derivatives(tmp_path):
    path = tmp_path / 'weights.json'
    rows = [
        {'usda_id': 'bad', 'food_name': 'Chives, freeze-dried', 'portions': [{'amount': 1, 'grams': 3.2, 'portion_desc': 'cup'}]},
        {'usda_id': 'fresh', 'food_name': 'Chives, raw', 'portions': [{'amount': 1, 'grams': 3, 'portion_desc': 'tablespoon'}]},
        {'usda_id': 'berries', 'food_name': 'Raspberries, raw', 'portions': [{'amount': 1, 'grams': 150, 'portion_desc': 'cup'}]},
        {'usda_id': 'nut', 'food_name': 'Nuts, hazelnuts or filberts', 'portions': [{'amount': 1, 'grams': 135, 'portion_desc': 'cup, whole'}]},
    ]
    path.write_text(json.dumps(rows))
    assert weights.find_weight_match_by_name('chives', 'cup', path) is None
    assert weights.find_weight_match_by_name('chives', 'tablespoon', path)['usda_id'] == 'fresh'
    assert weights.find_weight_match_by_name('raspberry', 'cup', path)['usda_id'] == 'berries'
    assert weights.find_weight_match_by_name('nut, hazelnut', 'cup', path)['usda_id'] == 'nut'


def test_metric_target_volume_uses_existing_cup_reference(tmp_path):
    path = tmp_path / 'weights.json'
    path.write_text(json.dumps([{'usda_id': 'berry', 'food_name': 'Raspberries, raw', 'portions': [{'amount': 1, 'grams': 150, 'portion_desc': 'cup'}]}]))
    units = tmp_path / 'units.json'
    units.write_text(json.dumps({'units_ml': {'cup': 237}}))
    for unit, quantity in [('ml', 100), ('dl', 1), ('l', .1)]:
        value = weights.weight_from_density_fallback('berry', unit, quantity, weights_path=path, unit_volumes_path=units)
        assert value['grams'] == pytest.approx(150 / 237 * 100)


def test_rejected_freeze_dried_cup_allows_existing_fresh_reference():
    with patch.object(tool, 'find_weight_match_by_name', return_value=None):
        result = tool._reference_weight_fallback('chives', '100', 'ml')
    assert result[0] == 20
    assert result[2] == 'reviewed_cup_density_fallback'


def test_qualifier_overlap_is_not_food_identity():
    assert not tool._candidate_has_lexical_identity('champignon, fresh', {'canonical': 'Parsley, fresh'})
    assert not tool._candidate_has_lexical_identity('lamb roast', {'canonical': 'Roast beef spread'})
    assert tool._candidate_has_lexical_identity('fresh raspberry', {'canonical': 'Raspberries, raw'})
    assert tool._usda_query_alias('champignon, fresh') == 'white mushroom, fresh'


@pytest.mark.parametrize('source,candidate', [
    ('strawberry', 'Toppings, strawberry'),
    ('cranberry', 'Beans, cranberry, cooked'),
    ('mustard', 'Mustard greens, raw'),
    ('blood', 'Blood sausage'),
    ('coffee', 'Cream, fluid, light (coffee cream or table cream)'),
    ('celeriac, turnip-rooted celery, knob celery, celery root', 'Turnips, raw'),
])
def test_portion_food_rejects_a_different_product_despite_shared_word(source, candidate):
    assert not weights.portion_food_form_compatible(source, candidate)


def test_substring_inside_food_word_cannot_supply_an_unrelated_portion(tmp_path):
    path = tmp_path / 'weights.json'
    path.write_text(json.dumps([
        {'usda_id': 'bad', 'food_name': 'Cucumber, peeled, raw',
         'portions': [{'amount': 1, 'grams': 201, 'portion_desc': 'medium'}]},
        {'usda_id': 'tea', 'food_name': 'Taro leaves, steamed',
         'portions': [{'amount': 1, 'grams': 145, 'portion_desc': 'cup'}]},
    ]))
    assert weights.find_weight_match_by_name('eel', 'medium', path) is None
    assert weights.find_weight_match_by_name('tea', 'cup', path) is None


def test_sized_whole_food_skips_cut_portions_but_explicit_wedge_can_use_them(tmp_path):
    path = tmp_path / 'weights.json'
    path.write_text(json.dumps([{
        'usda_id': 'melon', 'food_name': 'Watermelon, raw', 'portions': [
            {'amount': 1, 'grams': 325, 'portion_desc': 'medium wedge'},
            {'amount': 1, 'grams': 4518, 'portion_desc': 'medium melon'},
        ],
    }]))
    assert weights.match_portion('melon', 'medium', name='watermelon', weights_path=path)['grams_per_unit'] == 4518
    assert weights.match_portion('melon', 'medium', name='watermelon wedge', weights_path=path)['grams_per_unit'] == 325
    assert weights.weight_from_ingredient({'name': 'watermelon', 'usda_id': 'melon', 'quantity': 1, 'unit': 'medium'}, path) == 4518


def test_corn_tortilla_count_uses_existing_corn_reference_instead_of_flour_wrap():
    grams, portion, method = tool._reference_weight_fallback('corn tortillas', '8', 'tortilla')
    assert grams == 208
    assert '18449' in portion['portion_desc']
    assert method == 'common_unit_reference_fallback'
    assert tool._common_unit_reference_grams('flour tortillas', 'tortilla')[0] == 50


@pytest.mark.parametrize('name', ['celeriac', 'celery root', 'celeriac, turnip-rooted celery, knob celery, celery root'])
def test_celery_root_uses_root_convention_instead_of_stalk_or_turnip(name):
    with patch.object(tool, 'match_portion', return_value={'portion_desc': 'medium', 'grams_per_unit': 122}), \
         patch.object(tool, 'weight_food_name', return_value='Turnips, raw'):
        grams, portion, method = tool._reference_weight_fallback(name, '1', 'medium', usda_id='11564')
    assert grams == 500
    assert method == 'reviewed_item_weight_fallback'
    assert 'convention' in portion['portion_desc']


def test_bare_count_of_celery_root_names_whole_roots_not_stalks():
    assert tool._infer_unit_from_name('celery root') == 'whole'
    with patch.object(tool, 'canonical_name_to_usda', return_value=None):
        output = tool.ingredient_weight_tool_usda.invoke({'ingredient_names': ['celery root'], 'measurements': ['2'], 'return_details': True})
    assert output['weights'] == [1000]


def test_lamb_and_goat_are_protected_protein_identities():
    assert tool._usda_link_mismatch_reason('lamb roast', {'canonical': 'Roast beef spread'}) is not None
    assert tool._usda_link_mismatch_reason('goat meat', {'canonical': 'Pork, fresh'}) is not None


def test_shared_peeling_or_generic_nut_label_does_not_identify_food():
    assert not tool._candidate_has_lexical_identity('potato, peeled', {'canonical': 'Cucumber, peeled, raw'})
    assert not tool._candidate_has_lexical_identity('nut, hazelnut', {'canonical': 'Nuts, walnuts'})
    assert tool._candidate_has_lexical_identity('nut, hazelnut', {'canonical': 'Nuts, hazelnuts or filberts'})


def test_sheep_and_mutton_are_not_rejected_as_different_animals_from_lamb():
    assert tool._protein_identity_mismatch_reason('lamb roast', 'Mutton, raw') is None
    assert tool._protein_identity_mismatch_reason('sheep meat', 'Lamb, raw') is None
    assert tool._protein_identity_mismatch_reason('goat meat', 'Lamb, raw') is not None


def test_preparation_specific_portion_wins_and_plain_nuts_prefer_whole():
    portions = [
        {'portion_desc': 'cup, chopped', 'grams_per_unit': 115},
        {'portion_desc': 'cup, ground', 'grams_per_unit': 75},
        {'portion_desc': 'cup, whole', 'grams_per_unit': 135},
    ]
    assert weights._best_portion_for_name(portions, 'hazelnuts')['grams_per_unit'] == 135
    assert weights._best_portion_for_name(portions, 'ground hazelnuts')['grams_per_unit'] == 75
    assert weights._best_portion_for_name(portions, 'chopped hazelnuts')['grams_per_unit'] == 115


def test_density_matches_reported_preparation_instead_of_mixing_forms(tmp_path):
    units = tmp_path / 'units.json'
    units.write_text(json.dumps({'units_ml': {'cup': 237}}))
    portions = [{'portion_desc': 'cup, '+form, 'grams': grams, 'amount': 1}
                for form, grams in [('chopped', 115), ('ground', 75), ('whole', 135)]]
    for name, expected in [('hazelnuts', 135), ('ground hazelnuts', 75), ('chopped hazelnuts', 115)]:
        result = weights._density_from_portions(portions, name, units)
        assert result['density_g_per_ml'] == pytest.approx(expected / 237)
        assert result['source_grams_per_unit'] == expected
        assert result['density_g_per_ml'] == pytest.approx(result['source_grams_per_unit'] / result['source_unit_ml'])


def test_whole_roast_is_not_identified_as_ground_meat():
    assert not weights.portion_food_form_compatible('lamb roast', 'Lamb, ground, cooked')
    assert weights.portion_food_form_compatible('minced lamb', 'Lamb, ground, raw')


@pytest.mark.parametrize('description,expected', [('slice (1 oz)', 'slice'), ('small (2 inches)', 'small'), ('sprigs', 'sprigs'), ('1/2 cup, chopped', 'cup')])
def test_portion_unit_does_not_strip_leading_s_as_whitespace(description, expected):
    assert weights._portion_unit(description) == expected


def test_measurement_fallback_parses_actual_whitespace():
    assert weights._parse_measurement('1/2 cup') == ('1/2', 'cup')
    assert weights._parse_measurement('2 tablespoons') == ('2', 'tablespoons')


def test_reviewed_foundation_dried_egg_white_portion_is_form_and_unit_specific():
    from recipe_wrangler.tools.reviewed_item_weights import reviewed_item_grams
    value = reviewed_item_grams('egg, white, dried', 'tablespoon')
    assert value[0] == 7 and '323793' in value[1]
    assert reviewed_item_grams('dried egg whites', 'tablespoon')[0] == 7
    assert reviewed_item_grams('egg, white, dried', 'cup') is None
    assert reviewed_item_grams('egg, white, fresh', 'tablespoon') is None
    assert reviewed_item_grams('dried egg white baking mix', 'tablespoon') is None
    result = tool._reference_weight_fallback('egg, white, dried', '2', 'tablespoon')
    assert result[0] == 14
    assert '323793' in result[1]['portion_desc']


def test_raw_or_unspecified_food_does_not_inherit_smoked_or_pickled_weight():
    assert not weights.portion_food_form_compatible('herring', 'Herring, kippered')
    assert not weights.portion_food_form_compatible('herring, raw', 'Herring, pickled')
    assert weights.portion_food_form_compatible('smoked herring', 'Herring, kippered')
    assert tool._usda_link_mismatch_reason('baltic herring', {'canonical': 'Fish, herring, Atlantic, raw'}) == 'explicit_herring_species_mismatch'
    assert tool._usda_link_mismatch_reason('baltic herring', {'canonical': 'Fish, herring, Baltic, raw'}) is None


def test_volume_fallback_reports_the_actual_selected_food_form():
    reference = {'food_name': 'Nuts, hazelnuts', 'portion': {'portion_desc': 'cup, whole', 'grams_per_unit': 135}}
    with patch.object(tool, 'find_weight_match_by_name', return_value=reference):
        grams, detail, path = tool._reference_weight_fallback('hazelnuts', '100', 'ml')
    assert grams == 56.25
    assert detail['source_portion_desc'] == 'cup, whole'
    assert grams == pytest.approx(100 * detail['source_grams_per_unit'] / detail['source_unit_ml'])


def test_explicit_usda_size_precedes_generic_count_multiplier():
    portion = {'portion_desc': 'small (6 inches)', 'grams_per_unit': 101.0}
    with patch.object(tool, 'REVIEWED_TABLES_ENABLED', False), patch.object(tool, 'match_portion', return_value=portion), patch.object(tool, 'weight_food_name', return_value='Bananas, raw'):
        grams, detail, path = tool._reference_weight_fallback('banana', '2', 'small', usda_id='09040')
    assert grams == 202
    assert path == 'usda_explicit_size_reference'
    assert detail['source'] == 'usda_explicit_size'


@pytest.mark.parametrize('source,candidate', [
    ('carrot', 'Carrots, baby, raw'),
    ('turnip', 'Turnip greens, raw'),
    ('turnip greens', 'Turnips, raw'),
    ('egg yolk', 'Egg, whole, raw'),
    ('egg', 'Egg, white, raw'),
    ('potato, peeled', 'Potatoes, flesh and skin, raw'),
])
def test_size_references_cannot_use_a_different_maturity_or_food_part(source, candidate):
    assert tool._usda_link_mismatch_reason(source, {'canonical': candidate}) is not None


def test_explicit_size_does_not_override_missing_reference_with_fake_precision():
    with patch.object(tool, 'european_size_reference', return_value=None), patch.object(tool, 'match_portion', return_value=None):
        result = tool._reference_weight_fallback('banana', '1', 'small', usda_id='09040')
    assert result[2] == 'common_unit_reference_fallback'


def test_bare_size_row_cannot_override_named_bulb_reference():
    with patch.object(tool, 'match_portion', return_value={'portion_desc': 'large', 'grams_per_unit': 25}), patch.object(tool, 'weight_food_name', return_value='Fennel, bulb, raw'):
        result = tool._reference_weight_fallback('fennel bulb', '1', 'large', usda_id='11957')
    assert result[0] == pytest.approx(315.9)
    assert result[2] == 'common_unit_reference_fallback'


def test_maturity_and_shared_body_words_do_not_override_food_identity():
    assert tool._usda_link_mismatch_reason('courgette', {'canonical': 'Squash, zucchini, baby, raw'}) is not None
    assert not tool._candidate_has_lexical_identity('fennel bulb', {'canonical': 'Onions, spring, includes bulb'})
    assert tool._candidate_has_lexical_identity('fennel bulb', {'canonical': 'Fennel, bulb, raw'})


def test_canonical_alias_cannot_hide_wrong_food_behind_usda_id():
    with patch.object(tool, 'weight_food_name', return_value='Carrots, baby, raw'):
        reason = tool._usda_link_mismatch_reason('carrot', {'canonical': 'carrot', 'usda_id': '11683'})
    assert reason == 'portion_table_food_maturity_mismatch'
    with patch.object(tool, 'weight_food_name', return_value='Onions, spring or scallions, includes bulb, raw'):
        reason = tool._usda_link_mismatch_reason('fennel bulb', {'canonical': 'fennel bulb', 'usda_id': '11291'})
    assert reason is not None


def test_baby_back_is_a_rib_cut_not_produce_maturity():
    assert tool._usda_link_mismatch_reason('baby back ribs', {'canonical': 'Pork, back ribs, raw'}) is None


@pytest.mark.parametrize('name,expected,path', [
    ('potato, peeled', 70, 'reviewed_european_size_reference'),
    ('potato, peeled, boiled', 167, 'usda_explicit_size_reference'),
])
def test_cooked_size_row_requires_explicit_cooking_context(name, expected, path):
    with patch.object(tool, 'match_portion', return_value={'portion_desc': 'medium', 'grams_per_unit': 167}), patch.object(tool, 'weight_food_name', return_value='Potatoes, boiled, cooked without skin'):
        result = tool._reference_weight_fallback(name, '1', 'medium', usda_id='11367')
    assert result[0] == expected and result[2] == path


@pytest.mark.parametrize('query,stripped,candidate,accepted', [
    ('egg white', 'egg', 'Egg, whole, raw', False),
    ('fresh banana', 'banana', 'Bananas, raw', True),
])
def test_stripped_query_recovery_rechecks_original_food_identity(query, stripped, candidate, accepted):
    original = tool._embedding_usda_link.__wrapped__
    with patch.object(tool, '_get_usda_links_collections', return_value=[object()]), patch.object(tool, 'get_embeddings', return_value=[0., 1.]), patch.object(tool, '_lexical_usda_link_candidates', return_value=[]), patch.object(tool, '_strip_qualifiers', return_value=stripped), patch.object(tool, '_embedding_usda_link', return_value={'canonical': candidate}):
        result = original(query)
    assert (result is not None) == accepted


def test_name_based_portion_lookup_also_preserves_food_parts_and_maturity(tmp_path):
    path = tmp_path / 'parts.json'
    rows = [
        {'usda_id': 'leaf', 'food_name': 'Turnip greens, raw'},
        {'usda_id': 'part', 'food_name': 'Egg, white, raw'},
        {'usda_id': 'baby', 'food_name': 'Carrots, baby, raw'},
    ]
    for r in rows:r['portions'] = [{'portion_desc': 'cup', 'amount': 1, 'grams': 40}]
    path.write_text(json.dumps(rows))
    for query in ('turnip', 'egg', 'carrot'):
        assert weights.find_weight_match_by_name(query, 'cup', path) is None
    assert weights.find_weight_match_by_name('egg white', 'cup', path)['usda_id'] == 'part'


@pytest.mark.parametrize('query,label', [('peeled potato', 'Potatoes, peeled, raw'), ('red tomato', 'Tomatoes, red, raw'), ('radish', 'Radishes, raw'), ('sliced mango', 'Mangoes, sliced, raw')])
def test_shared_plural_rules_match_reordered_food_labels(tmp_path, query, label):
    path = tmp_path / 'plural.json'
    path.write_text(json.dumps([{'usda_id': 'food', 'food_name': label, 'portions': [{'amount': 1, 'grams': 100, 'portion_desc': 'cup'}]}]))
    assert weights.find_weight_match_by_name(query, 'cup', path)['usda_id'] == 'food'
    assert tool._candidate_has_lexical_identity(query, {'canonical': label})


def test_flaked_and_coconut_water_are_not_the_plain_grain_or_nut():
    assert not weights.portion_food_form_compatible('rye flakes', 'Rye grain')
    assert not weights.portion_food_form_compatible('coconut flake', 'Nuts, coconut water (liquid from coconuts)')
    assert weights.portion_food_form_compatible('rolled oat flakes', 'Oat flakes')
    assert weights.portion_food_form_compatible('coconut water', 'Nuts, coconut water (liquid from coconuts)')


def test_dried_dry_stored_staples_match_usda_raw_but_fruit_does_not():
    assert weights.portion_food_form_compatible('lentils, green/brown, dried', 'Lentils, raw')
    assert weights.portion_food_form_compatible('soy beans, dried', 'Soybeans, mature seeds, raw')
    assert not weights.portion_food_form_compatible('apple, dried', 'Apples, raw, with skin')


def test_uncooked_and_enriched_are_not_head_tokens():
    assert tool._head_token_mismatch_reason('rice, white', 'Rice, white, long-grain, regular, raw, unenriched') is None
    assert not weights.portion_food_form_compatible('rice, white', 'Rice, white, glutinous, unenriched, uncooked')
    assert not weights.portion_food_form_compatible('cooked rice', 'Rice, white, glutinous, unenriched, cooked')
    assert tool._head_token_mismatch_reason('rice, brown', 'Rice, brown, long-grain, raw, enriched') is None
    assert tool._head_token_mismatch_reason('rice', 'Bagels, plain') is not None


def test_generic_form_words_and_colours_are_not_food_identity():
    ident = tool._candidate_has_lexical_identity
    assert not ident('tomato puree', {'canonical': 'Prune puree'})
    assert not ident('rye flakes', {'canonical': 'Cereals ready-to-eat, POST Bran Flakes'})
    assert not ident('sesame seed', {'canonical': 'Seeds, hemp seed, hulled'})
    assert not ident('beetroot, red beet', {'canonical': 'Peppers, sweet, red, raw'})
    assert ident('tomato puree', {'canonical': 'Tomato products, canned, puree'})
    assert ident('sesame seed', {'canonical': 'Seeds, sesame seeds, whole, dried'})
    assert ident('red onion', {'canonical': 'Onions, raw'})


def test_staples_without_stated_state_never_resolve_to_cooked_or_variant_records():
    ok = weights.portion_food_form_compatible
    assert not ok('rice, white', 'Rice, white, medium-grain, enriched, cooked')
    assert not ok('rice, brown', 'Rice, brown, parboiled, cooked, UNCLE BENS')
    assert not ok('pasta', 'Pasta, gluten-free, corn, cooked')
    assert not ok('pasta', 'Pasta, gluten-free, corn, dry')
    assert ok('cooked rice', 'Rice, white, medium-grain, enriched, cooked')
    assert ok('gluten-free pasta', 'Pasta, gluten-free, corn, dry')
    assert ok('rice, white', 'Rice, white, long-grain, regular, raw, enriched')


def test_variety_words_change_the_food():
    ok = weights.portion_food_form_compatible
    assert not ok('brown rice', 'Rice, white, medium-grain, raw, unenriched')
    assert not ok('white rice', 'Rice, brown, long-grain, raw')
    assert not ok('cooked rice', 'Wild rice, cooked')
    assert not ok('spaghetti', 'Squash, winter, spaghetti, raw')
    assert ok('brown rice', 'Rice, brown, long-grain, raw')
    assert ok('wild rice', 'Wild rice, raw')
    assert ok('spaghetti squash', 'Squash, winter, spaghetti, raw')


def test_plain_eggs_are_raw_not_omelet_or_powder():
    ok = weights.portion_food_form_compatible
    assert not ok('eggs', 'Egg, whole, cooked, omelet')
    assert not ok('eggs', 'Egg, whole, dried')
    assert ok('eggs', 'Egg, whole, raw, fresh')
    assert ok('fried egg', 'Egg, whole, cooked, fried')


def test_milk_animal_must_be_stated():
    ok = weights.portion_food_form_compatible
    assert not ok('milk', 'Milk, sheep, fluid')
    assert not ok('milk', 'Milk, goat, fluid')
    assert not ok('milk', 'Milk, human, mature, fluid')
    assert ok('goat milk', 'Milk, goat, fluid')


def test_usda_program_suffix_is_not_a_head_token_and_zucchini_is_a_squash():
    assert tool._head_token_mismatch_reason('brown rice', "Rice, brown, long-grain, raw (Includes foods for USDA's Food Distribution Program)") is None
    ok = weights.portion_food_form_compatible
    assert ok('zucchini', 'Squash, summer, zucchini, includes skin, raw')
    assert not ok('spaghetti', 'Squash, winter, spaghetti, raw')
    assert not ok('milk', 'Milk, chocolate, fluid')
    assert not ok('milk', 'Milk, buttermilk, fluid')
    assert not ok('milk', 'Milk, evaporated, 2% fat')


def test_noodle_basket_is_a_reviewed_nest():
    from recipe_wrangler.tools.reviewed_item_weights import reviewed_item_grams
    assert reviewed_item_grams('egg noodles', 'baskets', 3.0)[0] == 60.0
    assert reviewed_item_grams('egg noodles', 'nests', 2.0)[0] == 60.0
