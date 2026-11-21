from recipe_wrangler.tools.parse_recipe_tool import (
    _split_salt_and_pepper_entries,
    split_salt_and_pepper_rows,
)
import pytest


@pytest.mark.parametrize('name', [
    'pepper and salt', 'Salt and pepper, to taste', 'salt and pepper (to taste)', 'salt/pepper', 'salt, pepper',
    'freshly ground black pepper & sea salt to season',
])
def test_seasoning_order_and_notes_do_not_prevent_split(name):
    names, measurements, matches, weights = split_salt_and_pepper_rows([name], ['to taste'])
    assert names == (['sea salt', 'black pepper'] if 'sea salt' in name else ['salt', 'black pepper'])
    assert matches == names
    assert measurements == ['', '']
    assert weights is None


@pytest.mark.parametrize('name', ['salt and pepper squid', 'red pepper and salt', 'salt and vinegar crisps'])
def test_compound_dishes_and_vegetable_peppers_are_not_split(name):
    assert split_salt_and_pepper_rows([name], ['100 g'])[0] == [name]


def test_stated_white_pepper_is_preserved():
    assert split_salt_and_pepper_rows(['white pepper & table salt (to taste)'], [''])[0] == ['table salt', 'white pepper']


def test_splits_quantity_free_salt_and_pepper_without_touching_other_foods():
    names, measurements, match_names, weights = split_salt_and_pepper_rows(
        ["olive oil", "salt and freshly ground black pepper", "salt and vinegar crisps"],
        ["1 tbsp", "salt and pepper, to taste", "100 g"],
        ["olive oil", "salt and black pepper", "salt and vinegar crisps"],
    )

    assert names == [
        "olive oil",
        "salt",
        "black pepper",
        "salt and vinegar crisps",
    ]
    assert measurements == ["1 tbsp", "", "", "100 g"]
    assert match_names == names
    assert weights is None


def test_divides_a_shared_numeric_measurement_and_weight_instead_of_doubling():
    names, measurements, match_names, weights = split_salt_and_pepper_rows(
        ["sea salt & cracked black pepper"],
        ["1 dash"],
        ["sea salt & black pepper"],
        [0.4],
    )

    assert names == ["sea salt", "black pepper"]
    assert measurements == ["0.5 dash", "0.5 dash"]
    assert match_names == names
    assert weights == [0.2, 0.2]


def test_reparser_entries_are_split_and_remain_aligned():
    entries = _split_salt_and_pepper_entries(
        [{
            "name": "salt and black pepper",
            "measurement": "1.0 dash",
            "display": "1 dash salt and black pepper",
            "note": "",
        }]
    )

    assert entries == [
        {"name": "salt", "measurement": "0.5 dash", "display": "0.5 dash", "note": ""},
        {"name": "black pepper", "measurement": "0.5 dash", "display": "0.5 dash", "note": ""},
    ]


def test_season_with_literal_seasoning_does_not_allocate_measured_mixtures():
    from recipe_wrangler.utils.seasoning_quantities import salt_and_pepper_parts
    assert salt_and_pepper_parts('Season with salt and pepper') == ('salt', 'black pepper')
    assert salt_and_pepper_parts('Season with sea salt and white pepper') == ('sea salt', 'white pepper')
    assert salt_and_pepper_parts('Season with 1 tsp salt and pepper') is None
    assert salt_and_pepper_parts('Season with sugar and pepper') is None
