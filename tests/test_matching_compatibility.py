from recipe_wrangler.matching.compatibility import compare_facets
from recipe_wrangler.matching.models import FoodFacets


def test_compatibility_reports_agreement_conflict_and_missing_information() -> None:
    mention = FoodFacets(
        identity="chickpeas",
        preparation_state="cooked",
        preservation_methods=frozenset({"canned"}),
        sodium_level="no_added_salt",
    )
    candidate = FoodFacets(
        identity="chickpeas, canned",
        preparation_state="raw",
        preservation_methods=frozenset({"canned"}),
    )

    result = compare_facets(mention, candidate)

    assert result.relation_for("identity") == "not_evaluated"
    assert result.relation_for("preparation_state") == "conflict"
    assert result.relation_for("preservation_methods") == "agree"
    assert result.relation_for("sodium_level") == "insufficient_information"
    assert result.hard_contradictions == ("preparation_state",)


def test_compatibility_does_not_infer_unstated_candidate_facets() -> None:
    result = compare_facets(
        FoodFacets(processes=frozenset({"ground"})),
        FoodFacets(),
    )

    assert result.relation_for("processes") == "insufficient_information"
    assert result.hard_contradictions == ()


def test_alternative_or_multi_value_facet_can_partially_agree() -> None:
    result = compare_facets(
        FoodFacets(preservation_methods=frozenset({"canned", "dried"})),
        FoodFacets(preservation_methods=frozenset({"canned"})),
    )

    assert result.relation_for("preservation_methods") == "partial_agreement"
    assert result.hard_contradictions == ()


def test_fresh_allows_unqualified_candidate_but_rejects_preserved_candidate() -> None:
    mention = FoodFacets(preservation_methods=frozenset({"fresh"}))

    unqualified = compare_facets(mention, FoodFacets())
    frozen = compare_facets(
        mention,
        FoodFacets(preservation_methods=frozenset({"frozen"})),
    )

    assert unqualified.relation_for("preservation_methods") == "agree"
    assert frozen.relation_for("preservation_methods") == "conflict"
    assert frozen.hard_contradictions == ("preservation_methods",)
