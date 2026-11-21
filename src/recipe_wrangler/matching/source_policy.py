"""WiseFood experimental source policy, separate from equivalence."""

from __future__ import annotations

from collections.abc import Sequence

from recipe_wrangler.matching.models import (
    FoodMention,
    MatchDecision,
    MatchProvenance,
)


class WiseFoodRegionalSourcePolicy:
    """Prefer equivalence quality, then target region, then retrieval evidence."""

    policy_id = "wisefood_regional_preference_v0_experimental"

    def select(
        self,
        mention: FoodMention,
        acceptable: Sequence[MatchDecision],
    ) -> MatchDecision:
        eligible = [
            decision
            for decision in acceptable
            if decision.status in {"exact", "acceptable"}
            and decision.candidate is not None
            and decision.candidate.source_id in {mention.target_region, "eu"}
        ]
        if not eligible:
            return MatchDecision(
                status="abstain",
                candidate=None,
                confidence=None,
                reasons=("no_exact_or_acceptable_candidate",),
                provenance=MatchProvenance(
                    matcher_version="wisefood_experimental_v0",
                    policy_id=self.policy_id,
                ),
            )

        selected = min(eligible, key=lambda decision: self._sort_key(mention, decision))
        candidate = selected.candidate
        assert candidate is not None
        source_reason = (
            "source_policy:target_region_preferred"
            if candidate.source_id == mention.target_region
            else "source_policy:eu_fallback"
        )
        return MatchDecision(
            status=selected.status,
            candidate=candidate,
            confidence=None,
            reasons=(*selected.reasons, source_reason),
            provenance=MatchProvenance(
                matcher_version="wisefood_experimental_v0",
                policy_id=self.policy_id,
                evidence=selected.provenance.evidence,
            ),
        )

    @staticmethod
    def _sort_key(
        mention: FoodMention, decision: MatchDecision
    ) -> tuple[int, int, float, int, str]:
        candidate = decision.candidate
        assert candidate is not None
        retrieval = candidate.retrieval
        return (
            0 if decision.status == "exact" else 1,
            0 if candidate.source_id == mention.target_region else 1,
            -(retrieval.fused_score if retrieval.fused_score is not None else -1.0),
            retrieval.rank if retrieval.rank is not None else 10**9,
            candidate.record_id,
        )
