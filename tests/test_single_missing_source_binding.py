from types import SimpleNamespace

from recipe_wrangler.matching.source_context import unambiguous_missing_source_bindings


def ingredient(position, name, source):
    return SimpleNamespace(position=position, name=name, original_text=source, source_evidence=None)


def test_recovers_one_named_blank_in_an_otherwise_exact_ordered_list():
    items = [ingredient(0, 'rice', '140g rice'),
             ingredient(1, 'low-fat creme fraiche', ''),
             ingredient(2, 'parsley', '2 tbsp parsley')]
    lines = ['140g rice', '2 tablespoons of low-fat crème fraiche', '2 tbsp parsley']

    bindings = unambiguous_missing_source_bindings(items, lines)

    assert bindings is not None
    assert [item['source_position'] for item in bindings] == [0, 1, 2]
    assert [item['source_text_recovered'] for item in bindings] == [False, True, False]


def test_rejects_changed_or_ambiguous_source():
    items = [ingredient(0, 'rice', '140g rice'), ingredient(1, 'creme fraiche', '')]
    assert unambiguous_missing_source_bindings(items, ['150g rice', '2 tbsp crème fraiche']) is None
    assert unambiguous_missing_source_bindings(items, ['140g rice', '2 tbsp yoghurt']) is None
    assert unambiguous_missing_source_bindings(items, ['140g rice', '140g rice']) is None
    assert unambiguous_missing_source_bindings(items, ['140g rice']) is None


def test_recovers_two_distinct_blanks():
    items = [ingredient(0, 'rice', ''), ingredient(1, 'creme fraiche', '')]
    bindings = unambiguous_missing_source_bindings(items, ['140g rice', '2 tbsp crème fraiche'])
    assert bindings is not None and all(item['source_text_recovered'] for item in bindings)


def test_rejects_ambiguous_multiple_blanks():
    items = [ingredient(0, 'tomato', ''), ingredient(1, 'tomato', '')]
    assert unambiguous_missing_source_bindings(items, ['1 tomato', '2 tomato']) is None


def test_recovers_unique_blank_after_complete_lines_were_reordered():
    items = [ingredient(0, 'rice', '140g rice'), ingredient(1, 'creme fraiche', ''),
             ingredient(2, 'carrots', '100g carrots')]
    lines = ['100g carrots', '2 tbsp crème fraiche', '140g rice']
    bindings = unambiguous_missing_source_bindings(items, lines)
    assert bindings is not None
    assert [item['source_position'] for item in bindings] == [2, 1, 0]


def test_reordered_recovery_rejects_ambiguous_food_and_repeated_source():
    items = [ingredient(0, 'rice', '140g rice'), ingredient(1, 'parsley', ''),
             ingredient(2, 'carrots', '100g carrots')]
    assert unambiguous_missing_source_bindings(items,
        ['100g carrots', '30g parsley root', '50g parsley leaves']) is None
    assert unambiguous_missing_source_bindings(items,
        ['100g carrots', '140g rice', '140g rice']) is None
