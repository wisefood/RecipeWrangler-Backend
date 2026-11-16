import pytest
from recipe_wrangler.tools import parse_recipe_tool as parser


@pytest.mark.parametrize('name,line,measurement', [
    ('cooking spray', 'cooking spray', ''),
    ('cooking spray', 'cooking spray', '1 spray'),
    ('cooking spray', 'Brand cooking spray', ''),
    ('cooking spray', 'Brand cooking spray', '1.0'),
    ('olive oil spray', 'olive oil spray', '1 spray'),
    ('olive oil', 'olive oil spray', '1 spray'),
    ('oil', 'oil spray', ''),
])
def test_product_form_does_not_supply_application_count(name, line, measurement):
    entries = [{'name': name, 'measurement': measurement, 'display': '', 'note': ''}]
    parser._apply_deterministic_overrides(entries, [line])
    assert entries[0]['measurement'] == entries[0]['display'] == ''


@pytest.mark.parametrize('source,amount,expected', [
    ('2 sprays cooking spray', '', '2.0 sprays'),
    ('2 sprays cooking spray', '2.0', '2.0 sprays'),
    ('1 can cooking spray', '1.0', '1.0 can'),
    ('½ small pear', '0.5', '0.5 small'),
    ('1⁄2 extra-large pear', '0.5', '0.5 extra large'),
    ('1 ½ small pears', '1.5', '1.5 small'),
    ('½ fresh basil leaves', '0.5', '0.5 leaves'),
    ('4-6 fresh mint leaves', '5.0', '5.0 leaf'),
    ('2 to 4 garlic cloves', '3.0', '3.0 cloves'),
    ('2-4 sprays cooking spray', '3.0', '3.0 sprays'),
])
def test_restores_fraction_size_or_explicit_count(source, amount, expected):
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'food', 'measurement': amount, 'display': source})
    assert value == expected


@pytest.mark.parametrize('source,amount', [
    ('4-6 leaves or 1 cup herbs', '5.0'),
    ('4-6 leaves', '4.0'),
    ('1-20 oz jar leaves', '10.5'),
    ('2-4 inches', '3.0'),
])
def test_count_range_does_not_borrow_alternatives_or_container_sizes(source, amount):
    value, reason = parser.restore_explicit_source_measurement(
        {'name': 'food', 'measurement': amount, 'display': source})
    assert reason != 'source_range_unit'


@pytest.mark.parametrize('name,source,expected', [
    ('garlic', '2 teaspoons garlic granules or 1 teaspoon garlic powder', 'garlic granules'),
    ('garlic', '2 teaspoons garlic powder', 'garlic powder'),
    ('garlic', '2 teaspoons granulated garlic', 'granulated garlic'),
    ('onion', '2 teaspoons powdered onion', 'powdered onion'),
    ('garlic', '2 cloves garlic or 1 teaspoon garlic powder', 'garlic'),
    ('garlic', '2 cloves garlic, not garlic powder', 'garlic'),
    ('garlic', '2 cloves garlic (garlic powder is not recommended)', 'garlic'),
    ('garlic', 'no garlic powder', 'garlic'),
    ('garlic', 'garlic, excluding garlic powder', 'garlic'),
])
def test_preserves_only_explicit_primary_food_form(name, source, expected):
    entries = [{'name': name, 'measurement': '2.0', 'display': '2', 'note': ''}]
    parser._apply_deterministic_overrides(entries, [source])
    assert entries[0]['name'] == expected


def test_repeated_food_identity_is_not_guessed_from_fuzzy_assignment():
    entries = [
        {'name': 'garlic', 'measurement': '2.0', 'display': '2', 'note': ''},
        {'name': 'garlic', 'measurement': '1.0', 'display': '1', 'note': ''},
    ]
    parser._apply_deterministic_overrides(entries, ['2 cloves garlic', '1 teaspoon garlic powder'])
    assert [entry['name'] for entry in entries] == ['garlic', 'garlic']
