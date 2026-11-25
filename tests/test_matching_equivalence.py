from recipe_wrangler.matching.equivalence import RuleBasedEquivalenceModel
from recipe_wrangler.matching.models import (
    FoodCandidate,
    FoodFacets,
    FoodMention,
    RetrievalEvidence,
)


def _predict(mention: FoodFacets, candidate: FoodFacets):
    return RuleBasedEquivalenceModel().predict(
        FoodMention("test", mention.identity or "", mention),
        FoodCandidate("1", "test", candidate.identity or "", candidate, RetrievalEvidence()),
    )


def test_equivalence_accepts_exact_identity_and_supported_facets() -> None:
    prediction = _predict(
        FoodFacets(identity="garlic", preparation_state="raw"),
        FoodFacets(identity="garlic", preparation_state="raw"),
    )

    assert prediction.status == "exact"
    assert prediction.confidence is None


def test_equivalence_accepts_lexical_variant_without_calling_it_exact() -> None:
    prediction = _predict(
        FoodFacets(identity="olive oil"),
        FoodFacets(identity="oil olive extra virgin"),
    )

    assert prediction.status == "acceptable"


def test_equivalence_accepts_contained_identity_with_compatible_facets() -> None:
    prediction = _predict(
        FoodFacets(identity="risotto rice", preparation_state="raw"),
        FoodFacets(
            identity="rice white italian arborio risotto raw",
            preparation_state="raw",
            colour="white",
        ),
    )

    # The candidate is more specific, so it is acceptable rather than exact.
    assert prediction.status == "acceptable"


def test_equivalence_rejects_hard_facet_conflict() -> None:
    prediction = _predict(
        FoodFacets(identity="chickpea", preparation_state="cooked"),
        FoodFacets(identity="chickpea", preparation_state="raw"),
    )

    assert prediction.status == "incompatible"
    assert prediction.reasons == ("hard_facet_conflict:preparation_state",)


def test_equivalence_requires_candidate_evidence_for_stated_facet() -> None:
    prediction = _predict(
        FoodFacets(identity="chickpea", preservation_methods=frozenset({"canned"})),
        FoodFacets(identity="chickpea"),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == (
        "facet:preservation_methods:insufficient_information",
    )


def test_equivalence_rejects_different_identity() -> None:
    prediction = _predict(
        FoodFacets(identity="pumpkin"),
        FoodFacets(identity="sunflower"),
    )

    assert prediction.status == "incompatible"


def test_equivalence_does_not_require_candidate_to_repeat_cutting_method() -> None:
    prediction = _predict(
        FoodFacets(identity="garlic", processes=frozenset({"crushed"})),
        FoodFacets(identity="garlic"),
    )

    assert prediction.status == "exact"


def test_equivalence_does_not_require_candidate_to_repeat_grinding() -> None:
    prediction = _predict(
        FoodFacets(identity="flaxseed", processes=frozenset({"ground"})),
        FoodFacets(identity="flaxseed"),
    )

    assert prediction.status == "exact"


def test_equivalence_does_not_silently_introduce_material_candidate_form() -> None:
    prediction = _predict(
        FoodFacets(identity="stock"),
        FoodFacets(identity="stock powder", physical_form="powder"),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == ("candidate_specific_form_unstated:powder",)


def test_equivalence_does_not_silently_introduce_candidate_food_part() -> None:
    prediction = _predict(
        FoodFacets(identity="egg"),
        FoodFacets(identity="egg white", food_part="egg_white"),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == ("candidate_specific_food_part_unstated:egg_white",)


def test_equivalence_does_not_silently_introduce_candidate_colour() -> None:
    prediction = _predict(
        FoodFacets(identity="pepper"),
        FoodFacets(identity="white pepper", colour="white"),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == ("candidate_specific_colour_unstated:white",)


def test_equivalence_does_not_silently_introduce_candidate_preservation() -> None:
    prediction = _predict(
        FoodFacets(identity="pepper"),
        FoodFacets(identity="pickled pepper", preservation_methods=frozenset({"pickled"})),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == ("candidate_specific_preservation_unstated:pickled",)


def test_equivalence_does_not_silently_introduce_preparation_state() -> None:
    prediction = _predict(
        FoodFacets(identity="beef"),
        FoodFacets(identity="cooked beef", preparation_state="cooked"),
    )

    assert prediction.status == "insufficient_information"
    assert prediction.reasons == ("candidate_specific_preparation_unstated:cooked",)


def test_equivalence_does_not_silently_introduce_cooking_method() -> None:
    prediction = _predict(
        FoodFacets(identity="chestnut"),
        FoodFacets(
            identity="baked chestnut",
            preparation_state="cooked",
            processes=frozenset({"baked"}),
        ),
    )

    assert prediction.status == "insufficient_information"


def test_garlic_salt_is_not_equivalent_to_garlic() -> None:
    prediction = _predict(
        FoodFacets(identity="garlic"),
        FoodFacets(identity="garlic salt"),
    )

    assert prediction.status == "insufficient_information"
