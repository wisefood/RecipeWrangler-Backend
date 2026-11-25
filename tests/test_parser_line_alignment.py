"""Each parsed entry keeps its own source line, in order; repeats and split lines stay distinct."""
from recipe_wrangler.tools import parse_recipe_tool as parser


def entry(name, measurement=''):
    return {'name': name, 'measurement': measurement, 'display': '', 'note': ''}


def test_repeated_food_binds_each_entry_to_its_own_line_in_order():
    lines = ['6 apples', '1 tsp ground cinnamon', 'For the crumble topping:', '150 g oats',
             '1 tsp ground cinnamon', '3 tbsp maple syrup', '3 tbsp maple syrup']
    entries = [entry('apples'), entry('ground cinnamon'), entry('oats'), entry('ground cinnamon'),
               entry('maple syrup'), entry('maple syrup')]
    assert parser.align_entries_to_lines(entries, lines) == [0, 1, 3, 4, 5, 6]


def test_heading_is_skipped_and_leaves_no_entry_bound():
    lines = ['For the sauce:', '2 tbsp oil']
    assert parser.align_entries_to_lines([entry('oil')], lines) == [1]


def test_salt_and_pepper_split_share_one_line_but_repeats_do_not():
    lines = ['1 onion', 'Salt and pepper, to taste', '2 tbsp oil', 'Salt and pepper, to taste']
    entries = [entry('onion'), entry('salt'), entry('black pepper'), entry('oil'), entry('salt'), entry('black pepper')]
    assert parser.align_entries_to_lines(entries, lines) == [0, 1, 1, 2, 3, 3]


def test_unmatched_entry_is_unbound_not_guessed():
    assert parser.align_entries_to_lines([entry('saffron')], ['2 eggs']) == [None]


def test_assignment_returns_the_shared_line_for_both_split_parts():
    lines = ['Salt and pepper, to taste']
    assert parser._assign_lines_to_entries([entry('salt'), entry('black pepper')], lines) == lines * 2


def test_metric_imperial_contradiction_is_flagged_not_chosen():
    flag = parser.metric_imperial_conflict('170g / 60 oz. of caster sugar')
    assert flag['kind'] == 'contradiction' and flag['metric'] == '170 g' and flag['imperial'] == '60 oz'
    assert parser.metric_imperial_conflict('200g / 4 oz. of mushrooms')['kind'] == 'contradiction'


def test_rounded_or_consistent_imperial_is_not_a_contradiction():
    assert parser.metric_imperial_conflict('50g / 2 oz. of flour')['kind'] == 'rounded_display'
    assert parser.metric_imperial_conflict('450g / 1lb beef') is None
    assert parser.metric_imperial_conflict('2 tbsp oil') is None


def test_entries_carry_line_index_and_conflict_only_when_present(monkeypatch):
    lines = ['170g / 60 oz. of caster sugar', '2 eggs']
    entries = [entry('caster sugar', '170.0 g'), entry('eggs', '2.0')]
    parser.attach_source_links(entries, lines)
    assert [e['source_line_index'] for e in entries] == [0, 1]
    assert entries[0]['source_conflict']['kind'] == 'contradiction' and 'source_conflict' not in entries[1]


def test_accents_plurals_and_modifier_words_do_not_break_binding():
    lines = ['100ml low-fat crème fraiche', '2 medium tomatoes', '1 medium-sized chicken, 1.5kg']
    entries = [entry('low-fat creme fraiche'), entry('tomato'), entry('whole chicken')]
    assert parser.align_entries_to_lines(entries, lines) == [0, 1, 2]
