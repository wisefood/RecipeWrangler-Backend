from recipe_wrangler.matching.composition_policy import CompositionSelectionPolicy
from recipe_wrangler.matching.models import (
    FoodCandidate,
    FoodFacets,
    FoodMention,
    MatchDecision,
    MatchProvenance,
    RetrievalEvidence,
)


def _decision(source: str, facets: FoodFacets, rank: int = 1) -> MatchDecision:
    return MatchDecision(
        status="acceptable",
        candidate=FoodCandidate(
            "record",
            source,
            facets.identity or "candidate",
            facets,
            RetrievalEvidence(rank=rank),
        ),
        confidence=None,
        reasons=("equivalent",),
        provenance=MatchProvenance("test", "equivalence_only"),
    )


def test_composition_policy_allows_unstated_cutting_process() -> None:
    mention = FoodMention("garlic", "garlic", FoodFacets(identity="garlic"), target_region="ie")
    chopped = _decision(
        "ie",
        FoodFacets(identity="garlic chopped", processes=frozenset({"chopped"})),
    )

    selected = CompositionSelectionPolicy().select(mention, [chopped])

    assert selected.candidate is not None


def test_composition_policy_rejects_unstated_cooking_state() -> None:
    mention = FoodMention("beef", "beef", FoodFacets(identity="beef"), target_region="ie")
    cooked = _decision(
        "ie",
        FoodFacets(identity="cooked beef", preparation_state="cooked"),
    )

    selected = CompositionSelectionPolicy().select(mention, [cooked])

    assert selected.status == "abstain"


def test_composition_policy_rejects_unstated_material_form() -> None:
    mention = FoodMention("tomato", "tomato", FoodFacets(identity="tomato"), target_region="ie")
    paste = _decision(
        "ie",
        FoodFacets(identity="tomato paste", physical_form="paste"),
    )

    selected = CompositionSelectionPolicy().select(mention, [paste])

    assert selected.status == "abstain"


def test_composition_policy_retains_regional_preference_among_safe_candidates() -> None:
    mention = FoodMention("garlic", "garlic", FoodFacets(identity="garlic"), target_region="ie")
    eu = _decision("eu", FoodFacets(identity="garlic"), rank=1)
    regional = _decision("ie", FoodFacets(identity="garlic"), rank=3)

    selected = CompositionSelectionPolicy().select(mention, [eu, regional])

    assert selected.candidate is not None
    assert selected.candidate.source_id == "ie"
