from recipe_wrangler.utils.food_identity import compare_identities


def test_nutrition_claim_words_do_not_change_food_identity() -> None:
    assert compare_identities(
        "cheddar cheese", "Cheese, Cheddar type, half fat"
    ).relation == "likely_equivalent"
    assert compare_identities(
        "natural peanut butter", "Peanut butter 100% peanuts wo salt"
    ).relation == "likely_equivalent"
    assert compare_identities(
        "whole oats", "Oats grain, wholemeal"
    ).relation == "likely_equivalent"


def test_identity_handles_word_order_and_facets_separately() -> None:
    result = compare_identities("olive oil", "Oil, olive, extra virgin")

    assert result.relation == "likely_equivalent"
    assert result.mention_key == ("oil", "olive")
    assert result.candidate_key == ("oil", "olive")


def test_identity_treats_slices_as_a_handling_descriptor() -> None:
    assert compare_identities("turkey", "Turkey slices").relation == "likely_equivalent"


def test_identity_recognizes_cross_locale_synonym() -> None:
    result = compare_identities("courgettes", "Zucchini, raw")

    assert result.relation == "synonym"
    assert result.mention_key == result.candidate_key == ("zucchini",)


def test_identity_keeps_partial_overlap_uncertain() -> None:
    result = compare_identities("vanilla paste", "Curry paste")

    assert result.relation == "insufficient_information"
    assert result.shared_tokens == ("paste",)


def test_identity_accepts_complete_multi_token_containment() -> None:
    result = compare_identities(
        "risotto rice",
        "Rice, white, Italian Arborio risotto, raw",
    )

    assert result.relation == "likely_equivalent"
    assert result.reasons == ("complete_mention_token_containment",)


def test_identity_does_not_use_containment_for_single_token_foods() -> None:
    assert compare_identities("butter", "Ghee, butter").relation == "insufficient_information"
    assert compare_identities("garlic", "Garlic salt").relation == "insufficient_information"


def test_identity_rejects_containment_with_an_extra_food_identity() -> None:
    for mention, candidate in (
        ("chicken breast", "Ham, chicken breast"),
        ("rice milk", "Rice pudding made with milk"),
        ("sirloin steak", "Horse sirloin steak"),
    ):
        assert compare_identities(mention, candidate).relation == "insufficient_information"


def test_identity_rejects_disjoint_foods() -> None:
    result = compare_identities("pumpkin seed oil", "Sunflower oil")

    assert result.relation == "insufficient_information"
    assert result.shared_tokens == ("oil",)


def test_identity_keeps_product_defining_words() -> None:
    for mention, candidate in (
        ("garlic", "garlic salt"),
        ("chicken", "chicken stock"),
        ("tomato", "tomato paste"),
        ("milk", "coconut milk"),
    ):
        assert compare_identities(mention, candidate).relation == "insufficient_information"


def test_identity_normalizes_broth_to_stock() -> None:
    assert compare_identities("chicken broth", "stock chicken").relation == "synonym"


def test_identity_ignores_explicit_source_record_prefix() -> None:
    result = compare_identities("chestnuts", "11330 - chestnut")

    assert result.relation == "likely_equivalent"


def test_identity_preserves_non_prefix_numbers() -> None:
    result = compare_identities("egg pasta", "2 egg dry pasta")

    assert "2" in result.candidate_key
