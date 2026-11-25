"""Regressions from the 11 Nov held-recipe reprofile (zero-weight chicken)."""
from recipe_wrangler.tools import parse_recipe_tool as parser
from recipe_wrangler.tools.reviewed_item_weights import reviewed_item_grams


def test_chicken_breast_fillet_unit_has_a_reviewed_weight():
    assert reviewed_item_grams('chicken breast fillet', 'fillet')[0] == 174.0
    assert reviewed_item_grams('chicken fillets', 'fillets')[0] == 174.0


def test_total_mass_note_overrides_count_unit():
    entry = {'name': 'chicken fillets', 'measurement': '4.0 fillets', 'display': '4', 'note': 'lean, around 520g'}
    assert parser.restore_explicit_source_measurement(entry)[0] == '520.0 g'


def test_each_mass_note_is_not_a_total():
    entry = {'name': 'chicken fillets', 'measurement': '4.0 fillets', 'display': '4', 'note': 'about 130g each'}
    assert parser.restore_explicit_source_measurement(entry)[0] != '130 g'


def test_bare_sized_pepper_is_a_bell_pepper_not_banana_pepper():
    from recipe_wrangler.tools.ingredient_weight_tool import _usda_query_alias
    assert _usda_query_alias('pepper') == 'bell pepper'
    assert _usda_query_alias('red pepper') == 'red bell pepper'
    assert _usda_query_alias('peppers') == 'bell pepper'
    for spice in ('black pepper', 'cayenne pepper', 'chilli pepper', 'white pepper', 'mild red pepper ground'):
        assert 'bell' not in _usda_query_alias(spice)


def test_sized_sweet_pepper_uses_usda_sweet_pepper_sizes():
    from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda as tool
    names = ['pepper', 'red pepper', 'red pepper', 'pepper', 'bell pepper']
    meas = ['1.0 medium', '0.5 medium', '1.0 small', '1.0 large', '1.0']
    result = tool.invoke({'ingredient_names': names, 'measurements': meas, 'return_details': True})
    assert result['weights'] == [120.0, 60.0, 78.0, 162.0, 120.0]
    assert not any('banana' in str(d.get('match') or '').lower() for d in result['details'])


import pytest


@pytest.mark.parametrize('entry,line,expected', [
    ({'name': 'lemon', 'measurement': '1.0', 'display': '1', 'note': 'zest'}, 'Zest 1 lemon', ('lemon zest', '6.0 g')),
    ({'name': 'orange zest', 'measurement': '1.0', 'display': '1', 'note': ''}, 'Zest of one orange', ('orange zest', '12.0 g')),
    ({'name': 'orange rind', 'measurement': '0.5', 'display': '0.5', 'note': 'grated'}, 'Grated rind of half an orange', ('orange zest', '6.0 g')),
    ({'name': 'lime', 'measurement': '', 'display': '', 'note': 'juice of half a lime'}, 'Juice of half a lime', ('lime juice', '22.0 g')),
    ({'name': 'limes', 'measurement': '1.5', 'display': '1½', 'note': 'juice of'}, 'Juice of 1½ limes', ('lime juice', '66.0 g')),
    ({'name': 'lemon', 'measurement': '0.5', 'display': '0.5', 'note': 'juiced'}, '0.5 lemon, juiced', ('lemon juice', '24.0 g')),
    ({'name': 'lemon', 'measurement': '1.0', 'display': '1', 'note': ''}, 'Juice 1 lemon', ('lemon juice', '48.0 g')),
])
def test_citrus_part_is_weighed_as_the_part(entry, line, expected):
    names, measurements, _ = parser.prepare_entries_for_weights([entry], [line])
    assert (names[0], measurements[0]) == expected


@pytest.mark.parametrize('entry,line', [
    ({'name': 'lemon', 'measurement': '2.0', 'display': '2', 'note': ''}, '2 lemons'),
    ({'name': 'lime juice', 'measurement': '25.0 ml', 'display': '25ml', 'note': 'fresh'}, '25ml fresh lime juice, half a lime'),
    ({'name': 'lemon juice', 'measurement': '1.0 teaspoon', 'display': '1 teaspoon', 'note': ''}, '1 teaspoon lemon juice'),
])
def test_whole_fruit_and_measured_juice_are_untouched(entry, line):
    names, measurements, _ = parser.prepare_entries_for_weights([entry], [line])
    assert (names[0], measurements[0]) == (entry['name'], entry['measurement'])


@pytest.mark.parametrize('line,parsed,expected', [
    ('0,2 g Salt', '0.0 g', '0.2 g'),
    ('0,5 g Pepper, black', '0.0 g', '0.5 g'),
    ('1,5 kg Pork', '1.0 kg', '1.5 kg'),
    ('1,000 g flour', '1000.0 g', '1000.0 g'),
])
def test_decimal_comma_amount_is_kept(line, parsed, expected):
    names, measurements, _ = parser.prepare_entries_for_weights(
        [{'name': line.split(' ', 2)[2], 'measurement': parsed, 'display': '', 'note': ''}], [line])
    assert measurements[0] == expected


def test_digit_zero_for_letter_o_is_read_as_ounces():
    assert parser.metric_imperial_conflict('180g / 60z red pepper')['kind'] == 'rounded_display'
    assert parser.metric_imperial_conflict('170g / 60 oz. of caster sugar')['kind'] == 'contradiction'


def test_paired_colour_counts_are_summed():
    entry = {'name': 'pepper', 'measurement': '1.0', 'display': '1.0', 'note': 'red and green, chopped'}
    assert parser.prepare_entries_for_weights([entry], ['1 red and 1 green pepper, chopped'])[1] == ['2.0']
    entry = {'name': 'peppers', 'measurement': '2.0', 'display': '2', 'note': '1 red and 1 green'}
    assert parser.prepare_entries_for_weights([entry], ['2 peppers, 1 red and 1 green'])[1] == ['2.0']


def test_stated_teaspoon_of_zest_is_weighed_as_teaspoons():
    entry = {'name': 'orange', 'measurement': '1.0', 'display': '1', 'note': 'you need 1 teaspoon zest'}
    assert parser.prepare_entries_for_weights([entry], ['1 orange (you need 1 teaspoon zest)'])[:2] == (['orange zest'], ['2.0 g'])


def test_reviewed_public_record_is_guarded_by_its_own_digest():
    from scripts.composition import apply_source_reprofile as apply
    from recipe_wrangler.matching.reviewed_aliases import reviewed_public_record
    from recipe_wrangler.repositories import matching_store as store
    cid = 'reviewed:low-salt-stock-nevo-1883-diluted'
    row = reviewed_public_record(cid)
    assert row is not None
    entries = [{'release_id': 'r', 'profile': {'details': [{'source_nutrition': 'irish', 'selected_candidate_id': cid}]}}]
    key = {'collection': apply.COLLECTIONS['irish'], 'candidate_id': cid}

    class NoDatabase:
        def execute(self, *a, **k):
            raise AssertionError('reviewed records must not be looked up in the release table')
    apply._compositions(NoDatabase(), entries, [{**key, 'digest': store.digest(row)}])
    with pytest.raises(ValueError):
        apply._compositions(NoDatabase(), entries, [{**key, 'digest': 'stale'}])


def test_metric_alternative_beats_an_imperial_range():
    entry = {'name': 'chicken', 'measurement': '1.0 lb', 'display': '3-4lb', 'note': 'medium-sized'}
    assert parser.prepare_entries_for_weights([entry], ['1 medium-sized chicken, around 3-4lb / 1.5kg'])[1] == ['1500.0 g']


def test_baby_potatoes_are_small_potatoes():
    entry = {'name': 'potatoes', 'measurement': '24.0', 'display': '24 baby', 'note': 'baby'}
    assert parser.prepare_entries_for_weights([entry], ['24 baby potatoes'])[1] == ['24.0 small']


def test_whole_chicken_keeps_gross_weight_for_the_with_bone_record():
    from recipe_wrangler.tools.ingredient_weight_tool import _whole_bird_edible_factor
    assert _whole_bird_edible_factor('whole chicken') is None
    assert _whole_bird_edible_factor('whole skinless chicken') == 0.434
    assert _whole_bird_edible_factor('whole duck') == 0.633


def test_salt_named_only_as_low_salt_is_not_an_ingredient():
    entries = [{'name': 'stock', 'measurement': '575.0 ml', 'display': '575ml', 'note': 'low salt vegetable stock cube'},
               {'name': 'salt', 'measurement': '575.0 ml', 'display': '', 'note': 'to taste'}]
    lines = ['575ml stock (1 low salt vegetable stock cube dissolved in 575ml hot water)']
    assert [e['name'] for e in parser.drop_phantom_seasoning(entries, lines)] == ['stock']
    real = [{'name': 'salt', 'measurement': '', 'display': '', 'note': 'to taste'}]
    assert parser.drop_phantom_seasoning(real, ['Salt and pepper, to taste', '600ml low salt stock']) == real


def test_metric_amount_cannot_come_from_a_line_without_digits():
    entry = {'name': 'salt', 'measurement': '575.0 ml', 'display': '', 'note': 'to taste'}
    assert parser.prepare_entries_for_weights([entry], ['Salt and pepper to taste'])[1] == ['optional']
    onion = {'name': 'onion', 'measurement': '1.0', 'display': 'one', 'note': ''}
    assert parser.prepare_entries_for_weights([onion], ['one onion'])[1] == ['1.0']


def test_low_salt_qualifier_does_not_bind_a_salt_entry():
    entries = [{'name': 'stock'}, {'name': 'salt'}, {'name': 'pepper'}]
    lines = ['575ml stock (1 low salt vegetable stock cube dissolved in 575ml hot water)', 'Salt and pepper to taste']
    assert parser.align_entries_to_lines(entries, lines) == [0, 1, 1]


def test_qualified_salt_in_an_entry_name_still_binds_its_line():
    entries = [{'name': 'salt'}, {'name': 'cold, unsalted butter'}, {'name': 'low salt vegetable stock'}]
    lines = ['0.25 teaspoon salt', '3 tablespoons cold, unsalted butter, cut into small pieces', '600ml low salt vegetable stock']
    assert parser.align_entries_to_lines(entries, lines) == [0, 1, 2]


@pytest.mark.parametrize('name,original,matched,expected', [
    ('leftover turkey', '500g / 17 ½ oz. of leftover turkey', 'Turkey, raw', 'cooked turkey'),
    ('cooked prawns', '200g cooked prawns or shrimps', 'Prawns, fresh,', 'cooked prawns'),
    ('potatoes', '450g / 1lb of cooked, mashed potatoes', 'Potatoes raw', 'cooked potatoes'),
    ('potato', '1 medium potato, cooked and peeled', 'Potato, peeled, raw', 'cooked potatoes'),
    ('turkey breast', '900g cooked turkey breast', 'Turkey, breast, cooked in pan, no salt, no fat added', None),
    ('turkey', '4 slices turkey, thinly sliced', 'Turkey, raw', None),
])
def test_stated_cooked_state_replaces_a_raw_match(name, original, matched, expected):
    from recipe_wrangler.matching.reviewed_aliases import stated_state_name, REVIEWED_SELECTIONS
    assert stated_state_name(name, original, matched) == expected
    if expected:
        assert expected in REVIEWED_SELECTIONS


def test_alignment_handles_typo_plural_and_pinch_lines():
    assert parser.align_entries_to_lines([{'name': 'parsley'}], ['1 tablespoon fresh parlsey']) == [0]
    assert parser.align_entries_to_lines([{'name': 'peach juice'}], ['1/2 of the juice from the can of peaches']) == [0]
    entries = [{'name': 'salt'}, {'name': 'black pepper'}]
    assert parser.align_entries_to_lines(entries, ['Pinch of salt and pepper']) == [0, 0]
    assert parser.align_entries_to_lines([{'name': 'parsley'}], ['1 tablespoon fresh basil']) == [None]
