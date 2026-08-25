from recipe_wrangler.utils.diet_tags import (
    ALLERGEN_ABSENCE_RULES,
    DIET_TAG_NAMES,
    compute_diet_tags,
)


def test_v4_diet_vocabulary_is_closed_and_agreed() -> None:
    assert DIET_TAG_NAMES == (
        "vegan",
        "vegetarian",
        "pescatarian_safe",
        "nut_free",
        "dairy_free",
        "gluten_free",
        "gluten_free_option",
    )


def test_only_true_allergen_absence_tags_are_computed_from_allergens() -> None:
    assert set(ALLERGEN_ABSENCE_RULES) == {
        "nut_free",
        "dairy_free",
        "gluten_free",
    }
    assert compute_diet_tags(set()) == [
        "nut_free",
        "dairy_free",
        "gluten_free",
    ]


def test_allergen_absence_rules_block_their_own_restrictions() -> None:
    assert compute_diet_tags({"tree_nut", "milk", "wheat"}) == []
