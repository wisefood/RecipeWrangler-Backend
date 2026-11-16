import pytest
from recipe_wrangler.tools import parse_recipe_tool as parser


@pytest.mark.parametrize('source,measurement,expected', [
    ('3 extra large eggs', '3.0', '3.0 extra large'),
    ('3 extra-large eggs', '3.0', '3.0 extra large'),
    ('3 organic extra large eggs', '3.0', '3.0 extra large'),
    ('3 extra large eggs', '3.0 large', '3.0 extra large'),
    ('3 extra large eggs', '3.0 extra large', '3.0 extra large'),
    ('4 very small potatoes', '4.0', '4.0 very small'),
    ('4 extra-small potatoes', '4.0', '4.0 extra small'),
    ('3 large eggs', '3.0', '3.0 large'),
    ('3 medium eggs', '3.0', '3.0 medium'),
    ('3 extra large cloves garlic', '3.0', '3.0 cloves'),
    ('3 extra-large cloves garlic', '3.0', '3.0 cloves'),
])
def test_preserves_compound_sizes_and_prefers_explicit_count_units(source, measurement, expected):
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'food', 'measurement': measurement, 'display': source})
    assert value == expected
    assert reason == 'display_count'


def test_source_restoration_preserves_compound_size():
    entries = [{'name': 'eggs', 'measurement': '3.0', 'display': '3', 'note': 'extra large'}]
    parser._apply_deterministic_overrides(entries, ['3 extra large eggs'])
    assert entries[0] == {'name': 'eggs', 'measurement': '3.0 extra large', 'display': '3', 'note': 'extra large'}


def test_size_on_alternative_does_not_enlarge_primary():
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'eggs', 'measurement': '3.0', 'display': '3 medium eggs or 2 extra large eggs'})
    assert value == '3.0 medium'


@pytest.mark.parametrize('size,expected', [
    ('extra large', 'large'), ('extra-large', 'large'),
    ('very small', 'small'), ('extra large eggs', 'egg'),
])
def test_compound_size_remains_compatible_with_existing_weight_size_bands(size, expected):
    from recipe_wrangler.tools.ingredient_weight_tool import _clean_unit
    assert _clean_unit(size) == expected
