import pytest

from recipe_wrangler.utils.foodon_matching import (
    match_ingredient_to_foodon,
    normalize_foodon_label,
)


def test_normalize_foodon_label_singularizes_peaches() -> None:
    assert normalize_foodon_label("peaches") == "peach"


def test_reviewed_firm_tofu_alias_precedes_embedding() -> None:
    assert match_ingredient_to_foodon("firm tofu", {}) == (
        "FOODON_00005540",
        "reviewed_exact_alias",
        1.0,
        False,
    )


def test_reviewed_mixed_vegetables_alias_precedes_embedding() -> None:
    assert match_ingredient_to_foodon("mixed vegetables", {}) == (
        "FOODON_00002683",
        "reviewed_exact_alias",
        1.0,
        False,
    )


@pytest.mark.parametrize(
    ("name", "foodon_id", "method"),
    [
        ("fat-free plain yogurt", "FOODON_00001014", "reviewed_food_family_alias"),
        ("reduced-fat mozzarella", "FOODON_03303578", "reviewed_food_family_alias"),
        ("crumbled reduced-fat feta", "FOODON_00001256", "reviewed_food_family_alias"),
        ("1% low-fat milk", "FOODON_00001256", "reviewed_food_family_alias"),
        ("mixed salad greens", "FOODON_03310789", "reviewed_food_family_alias"),
        ("baby carrots", "FOODON_00001261", "reviewed_broad_group_alias"),
        ("fresh apricots", "FOODON_03315615", "reviewed_broad_group_alias"),
        ("whole wheat flour tortillas", "FOODON_00001709", "reviewed_broad_group_alias"),
    ],
)
def test_reviewed_family_aliases_precede_embedding(
    monkeypatch, name, foodon_id, method
) -> None:
    monkeypatch.setattr(
        "recipe_wrangler.utils.foodon_matching.match_embedding",
        lambda *_args, **_kwargs: pytest.fail("embedding should not be called"),
    )

    result = match_ingredient_to_foodon(name, {})

    assert result is not None
    assert result[0] == foodon_id
    assert result[1] == method
