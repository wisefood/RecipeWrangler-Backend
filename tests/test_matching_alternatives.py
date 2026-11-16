import pytest

from recipe_wrangler.matching.alternatives import select_first_alternative


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("risotto or arborio rice", "risotto rice"),
        ("orange or lemon juice", "orange juice"),
        ("green or red pepper", "green pepper"),
        ("orange or red bell pepper", "orange bell pepper"),
        ("red or green or yellow pepper", "red pepper"),
        ("vegetable or beef stock", "vegetable stock"),
        ("beef or chicken stock", "beef stock"),
        ("either margarine or butter", "margarine"),
        ("chicken or mushroom soup", "chicken soup"),
        ("sunflower or olive oil", "sunflower oil"),
        ("sirloin or fillet steak", "sirloin steak"),
        ("white fish fillet or steak", "white fish fillet"),
        ("vegetable or chicken stock cube", "vegetable stock cube"),
        ("wholewheat fusilli or penne pasta", "wholewheat fusilli pasta"),
        ("passata or tomato based pasta sauce", "passata"),
        ("chillies or chilli powder", "chillies"),
        ("Chive or spring onion", "Chive"),
        ("Chanterelle or girolle mushroom, raw", "Chanterelle mushroom"),
        (
            "Broth or stock, beef, dehydrated and reconstituted",
            "Broth, beef, dehydrated and reconstituted",
        ),
    ),
)
def test_selects_first_alternative_and_restores_shared_head(name: str, expected: str) -> None:
    selection = select_first_alternative(name)

    assert selection is not None
    assert selection.selected == expected
    assert selection.policy == "first_listed_default"


def test_flattened_name_uses_only_first_source_branch_for_facets() -> None:
    selection = select_first_alternative("peas", "300g fresh or frozen peas")

    assert selection is not None
    assert selection.selected == "peas"
    assert selection.source_context == "300g fresh peas"


def test_parenthesized_alternative_selects_the_first_branch() -> None:
    selection = select_first_alternative(
        "mixed berries", "700g fresh (or unsweetened frozen) mixed berries"
    )

    assert selection is not None
    assert selection.source_context == "700g fresh mixed berries"


def test_named_choice_keeps_first_branch_qualifiers() -> None:
    selection = select_first_alternative(
        "ham or bacon", "150g leftover ham or bacon, chopped"
    )

    assert selection is not None
    assert selection.selected == "ham"
    assert selection.source_context == "150g leftover ham"


def test_persisted_parser_selection_wins_over_text_fallback() -> None:
    selection = select_first_alternative(
        "chicken or turkey",
        persisted_alternatives=("chicken breast", "turkey breast"),
        persisted_selected="chicken breast",
    )

    assert selection is not None
    assert selection.selected == "chicken breast"


def test_instructional_or_is_not_an_alternative() -> None:
    assert select_first_alternative("rice", "cook rice or until tender") is None
    assert select_first_alternative("cook rice or until tender") is None
    assert select_first_alternative("milk or more") is None
