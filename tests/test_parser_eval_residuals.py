import pytest
from recipe_wrangler.tools import parse_recipe_tool as parser


@pytest.mark.parametrize('name,source', [('ice', 'ice cube'), ('ice', 'ice cubes'),
    ('ice cube', 'ice cube'), ('ice', 'ice cube (optional)')])
def test_ice_form_does_not_imply_one(name, source):
    entries = [dict(name=name, measurement='1.0 cube', display='1 cube', note='')]
    parser._apply_deterministic_overrides(entries, [source])
    assert entries[0]['measurement'] == entries[0]['display'] == ''


@pytest.mark.parametrize('source', ['2 ice cubes', 'a single ice cube',
    'ice cubes (2 per glass)', 'ice cube tray', 'ice cube or 1 cup water'])
def test_ice_guard_preserves_explicit_or_ambiguous_sources(source):
    entries = [dict(name='ice', measurement='2.0 cube', display='2 cubes', note='')]
    parser._apply_deterministic_overrides(entries, [source])
    assert entries[0]['measurement'] == '2.0 cube'


def test_repeated_ice_sources_are_not_assigned_quantity_free_by_guessing():
    entries = [dict(name='ice', measurement='1.0 cube', display='1 cube', note='')]
    parser._apply_deterministic_overrides(entries, ['ice cube', '2 ice cubes'])
    assert entries[0]['measurement'] == '1.0 cube'


@pytest.mark.parametrize('source', ['3 vegetable bouillon cubes', '3 sugar cubes'])
def test_explicit_cube_count_keeps_unit(source):
    result, _ = parser.restore_explicit_source_measurement(
        dict(name='food', measurement='3.0', display=source, note=''))
    assert result == '3.0 cubes'


@pytest.mark.parametrize('source', ['2 carrots or bouillon cubes',
    '2 carrots (serve with bouillon cubes)', '2 carrots, bouillon cubes',
    '2 carrots without bouillon cubes'])
def test_cube_unit_is_not_borrowed_from_another_food(source):
    result, _ = parser.restore_explicit_source_measurement(
        dict(name='carrots', measurement='2.0', display=source, note=''))
    assert result is None


@pytest.mark.parametrize('purpose', ['for coating', 'to dredge'])
def test_merged_portion_retains_its_explicit_purpose_and_amount(purpose):
    entries = [dict(name='flour', measurement='1.5 cups', display='1 1/2 cups', note='all-purpose')]
    parser._apply_deterministic_overrides(entries,
        ['1 cup all-purpose flour, plus', f'1/2 cup all-purpose flour {purpose}'])
    assert entries[0]['note'] == f'all-purpose; 1/2 cup {purpose}'
    assert entries[0]['measurement'] == '1.5 cups'


@pytest.mark.parametrize('lines,measurement', [
    (['1 cup flour', '1/2 cup flour for coating'], '1.5 cups'),
    (['1 cup flour, plus', '1/2 cup sugar for coating'], '1.5 cups'),
    (['1 cup flour, plus', '1/2 cup flour for coating'], '1.0 cup'),
    (['1 cup flour, plus', '1/2 tablespoon flour for coating'], '1.5 cups'),
    (['1 cup flour, plus', '1 egg', '1/2 cup flour for coating'], '1.5 cups'),
    (['1 cup flour, plus', '1/2 cup flour for coating or sauce'], '1.5 cups'),
])
def test_ambiguous_or_unsummed_portion_does_not_add_purpose(lines, measurement):
    entries = [dict(name='flour', measurement=measurement, display='', note='')]
    parser._preserve_merged_source_purposes(entries, lines)
    assert entries[0]['note'] == ''


def test_existing_merged_purpose_is_not_duplicated():
    entries = [dict(name='flour', measurement='1.5 cups', display='', note='for coating')]
    parser._preserve_merged_source_purposes(entries,
        ['1 cup flour, plus', '1/2 cup flour for coating'])
    assert entries[0]['note'] == 'for coating'
