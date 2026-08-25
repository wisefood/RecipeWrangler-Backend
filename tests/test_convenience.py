from recipe_wrangler.utils.convenience import (
    CONVENIENCE_TAG_NAMES,
    compute_convenience_tags,
)


def test_convenience_vocabulary_uses_product_facing_names() -> None:
    assert CONVENIENCE_TAG_NAMES == ("quick", "simple")


def test_quick_and_simple_thresholds_are_inclusive() -> None:
    assert compute_convenience_tags(30, 5) == ["quick", "simple"]


def test_unknown_placeholder_zero_is_not_convenient() -> None:
    assert compute_convenience_tags(0, 0) == []


def test_tags_are_computed_independently() -> None:
    assert compute_convenience_tags(31, 5) == ["simple"]
    assert compute_convenience_tags(30, 6) == ["quick"]
