from recipe_wrangler.matching.source_policy import WiseFoodRegionalSourcePolicy
from recipe_wrangler.matching.models import (
    FoodCandidate,
    FoodFacets,
    FoodMention,
    MatchDecision,
    MatchProvenance,
    RetrievalEvidence,
)


def _decision(status: str, source: str, record_id: str, score: float) -> MatchDecision:
    return MatchDecision(
        status=status,  # type: ignore[arg-type]
        candidate=FoodCandidate(
            record_id,
            source,
            record_id,
            FoodFacets(identity="beans"),
            RetrievalEvidence(rank=1, fused_score=score),
        ),
        confidence=None,
        reasons=(f"identity:{status}",),
        provenance=MatchProvenance("experimental"),
    )


def _mention() -> FoodMention:
    return FoodMention(
        "beans", "beans", FoodFacets(identity="beans"), target_region="irish"
    )


def test_exact_eu_beats_acceptable_regional_candidate() -> None:
    selected = WiseFoodRegionalSourcePolicy().select(
        _mention(),
        [_decision("acceptable", "irish", "regional", 0.99), _decision("exact", "eu", "eu", 0.80)],
    )

    assert selected.candidate is not None
    assert selected.candidate.record_id == "eu"


def test_regional_source_wins_between_equal_equivalence_classes() -> None:
    selected = WiseFoodRegionalSourcePolicy().select(
        _mention(),
        [_decision("exact", "eu", "eu", 0.99), _decision("exact", "irish", "regional", 0.80)],
    )

    assert selected.candidate is not None
    assert selected.candidate.record_id == "regional"
    assert "source_policy:target_region_preferred" in selected.reasons


def test_similarity_breaks_tie_only_after_equivalence_and_source() -> None:
    selected = WiseFoodRegionalSourcePolicy().select(
        _mention(),
        [_decision("exact", "irish", "lower", 0.80), _decision("exact", "irish", "higher", 0.90)],
    )

    assert selected.candidate is not None
    assert selected.candidate.record_id == "higher"


def test_policy_abstains_without_acceptable_candidate() -> None:
    selected = WiseFoodRegionalSourcePolicy().select(_mention(), [])

    assert selected.status == "abstain"
    assert selected.candidate is None
