"""Experimental composition-table policy over shared facet evidence."""

from __future__ import annotations

from collections.abc import Sequence

from recipe_wrangler.matching.source_policy import (
    WiseFoodRegionalSourcePolicy,
)
from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.models import (
    FoodCandidate,
    FoodMention,
    MatchDecision,
    MatchProvenance,
)


_HANDLING_PROCESSES = frozenset(
    {"chopped", "crushed", "diced", "drained", "grated", "ground", "minced", "peeled", "rinsed", "sliced"}
)
_MATERIAL_FORMS = frozenset(
    {"cube", "flakes", "granules", "juice", "paste", "powder", "puree", "sauce", "syrup"}
)
_STRICT_SCALAR_FACETS = (
    "biological_source",
    "food_part",
    "preparation_state",
    "colour",
    "sodium_level",
    "fat_level",
    "sugar_level",
    "grain_refinement",
    "skin_status",
    "bone_status",
    "packing_medium",
    "gluten_free",
)


def unsupported_composition_specificity(
    mention: FoodMention, candidate: FoodCandidate
) -> tuple[str, ...]:
    """Return unstated candidate details that can materially change nutrients."""
    unsupported: list[str] = []
    identity = compare_identities(
        mention.facets.base_identity or mention.facets.identity,
        candidate.facets.base_identity or candidate.facets.identity,
    )
    complete_identity_containment = (
        "complete_mention_token_containment" in identity.reasons
    )
    for facet in _STRICT_SCALAR_FACETS:
        stated = getattr(mention.facets, facet)
        candidate_value = getattr(candidate.facets, facet)
        if stated in (None, "unknown") and candidate_value not in (None, "unknown"):
            if facet == "colour" and complete_identity_containment:
                continue
            unsupported.append(f"{facet}:{candidate_value}")

    extra_processes = (
        candidate.facets.processes
        - mention.facets.processes
        - _HANDLING_PROCESSES
    )
    unsupported.extend(f"processes:{value}" for value in sorted(extra_processes))

    extra_preservation = (
        candidate.facets.preservation_methods
        - mention.facets.preservation_methods
    )
    unsupported.extend(
        f"preservation_methods:{value}" for value in sorted(extra_preservation)
    )

    candidate_form = candidate.facets.physical_form
    if (
        mention.facets.physical_form in (None, "unknown")
        and candidate_form in _MATERIAL_FORMS
    ):
        unsupported.append(f"physical_form:{candidate_form}")
    return tuple(unsupported)


class CompositionSelectionPolicy:
    """Apply nutrition-safety constraints, then WiseFood source preference."""

    policy_id = "wisefood_composition_safety_v0_experimental"

    def select(
        self,
        mention: FoodMention,
        acceptable: Sequence[MatchDecision],
    ) -> MatchDecision:
        safe = [
            decision
            for decision in acceptable
            if decision.candidate is not None
            and not unsupported_composition_specificity(mention, decision.candidate)
        ]
        selected = WiseFoodRegionalSourcePolicy().select(mention, safe)
        if selected.status == "abstain":
            return MatchDecision(
                status="abstain",
                candidate=None,
                confidence=None,
                reasons=("no_nutritionally_safe_equivalent_candidate",),
                provenance=MatchProvenance(
                    matcher_version="wisefood_experimental_v0",
                    policy_id=self.policy_id,
                ),
            )
        return MatchDecision(
            status=selected.status,
            candidate=selected.candidate,
            confidence=selected.confidence,
            reasons=(*selected.reasons, "composition_policy:nutrition_safe_facets"),
            provenance=MatchProvenance(
                matcher_version="wisefood_experimental_v0",
                policy_id=self.policy_id,
                evidence=selected.provenance.evidence,
            ),
        )
