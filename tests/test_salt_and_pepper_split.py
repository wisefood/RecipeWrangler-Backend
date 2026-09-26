from recipe_wrangler.tools.parse_recipe_tool import (
    _split_salt_and_pepper_entries,
    split_salt_and_pepper_rows,
)


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
