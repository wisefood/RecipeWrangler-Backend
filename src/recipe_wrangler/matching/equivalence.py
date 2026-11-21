"""Conservative deterministic equivalence classifier for experiments only."""

from __future__ import annotations

from recipe_wrangler.matching.compatibility import compare_facets
from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.models import (
    EquivalencePrediction,
    FoodCandidate,
    FoodFacets,
    FoodMention,
)


_SCALAR_FACETS = (
    "biological_source",
    "food_part",
    "preparation_state",
    "physical_form",
    "colour",
    "sodium_level",
    "fat_level",
    "sugar_level",
    "grain_refinement",
    "skin_status",
    "bone_status",
    "packing_medium",
    "gluten_free",
    "compound",
)
_NON_BLOCKING_PROCESSES = frozenset(
    {"chopped", "crushed", "diced", "drained", "grated", "ground", "minced", "peeled", "rinsed", "sliced"}
)
_MATERIAL_FORMS = frozenset(
    {"cube", "flakes", "granules", "juice", "paste", "powder", "puree", "sauce", "syrup"}
)


class RuleBasedEquivalenceModel:
    """Combine identity and facet evidence without source or rank preference."""

    model_id = "wisefood_rule_equivalence_v2_experimental"

    def predict(
        self, mention: FoodMention, candidate: FoodCandidate
    ) -> EquivalencePrediction:
        identity = compare_identities(
            mention.facets.base_identity or mention.facets.identity,
            candidate.facets.base_identity or candidate.facets.identity,
        )
        compatibility = compare_facets(mention.facets, candidate.facets)

        if identity.relation == "different":
            return EquivalencePrediction(
                "incompatible", None, ("identity:different", *identity.reasons)
            )
        if compatibility.hard_contradictions:
            return EquivalencePrediction(
                "incompatible",
                None,
                tuple(
                    f"hard_facet_conflict:{facet}"
                    for facet in compatibility.hard_contradictions
                ),
            )
        if identity.relation == "insufficient_information":
            return EquivalencePrediction(
                "insufficient_information",
                None,
                ("identity:insufficient_information", *identity.reasons),
            )

        if (
            mention.facets.physical_form in (None, "unknown")
            and candidate.facets.physical_form in _MATERIAL_FORMS
        ):
            return EquivalencePrediction(
                "insufficient_information",
                None,
                (f"candidate_specific_form_unstated:{candidate.facets.physical_form}",),
            )
        complete_identity_containment = (
            "complete_mention_token_containment" in identity.reasons
        )
        for facet in ("food_part", "colour"):
            mention_value = getattr(mention.facets, facet)
            candidate_value = getattr(candidate.facets, facet)
            if mention_value in (None, "unknown") and candidate_value not in (None, "unknown"):
                if facet == "colour" and complete_identity_containment:
                    continue
                return EquivalencePrediction(
                    "insufficient_information",
                    None,
                    (f"candidate_specific_{facet}_unstated:{candidate_value}",),
                )
        if (
            not mention.facets.preservation_methods
            and candidate.facets.preservation_methods
        ):
            methods = ",".join(sorted(candidate.facets.preservation_methods))
            return EquivalencePrediction(
                "insufficient_information",
                None,
                (f"candidate_specific_preservation_unstated:{methods}",),
            )
        if (
            mention.facets.preparation_state in (None, "unknown")
            and candidate.facets.preparation_state not in (None, "unknown")
        ):
            return EquivalencePrediction(
                "insufficient_information",
                None,
                (
                    "candidate_specific_preparation_unstated:"
                    f"{candidate.facets.preparation_state}"
                ,),
            )
        candidate_cooking = candidate.facets.processes - _NON_BLOCKING_PROCESSES
        if not (mention.facets.processes - _NON_BLOCKING_PROCESSES) and candidate_cooking:
            methods = ",".join(sorted(candidate_cooking))
            return EquivalencePrediction(
                "insufficient_information",
                None,
                (f"candidate_specific_process_unstated:{methods}",),
            )
        if (
            mention.facets.sodium_level in (None, "unknown")
            and candidate.facets.sodium_level not in (None, "unknown")
        ):
            return EquivalencePrediction(
                "insufficient_information",
                None,
                (f"candidate_specific_sodium_unstated:{candidate.facets.sodium_level}",),
            )

        unresolved: list[str] = []
        partial: list[str] = []
        for facet in _stated_facets(mention.facets):
            relation = compatibility.relation_for(facet)
            if relation in {"insufficient_information", "conflict"}:
                unresolved.append(f"facet:{facet}:{relation}")
            elif relation == "partial_agreement":
                partial.append(f"facet:{facet}:partial_agreement")
        if unresolved:
            return EquivalencePrediction(
                "insufficient_information", None, tuple(unresolved)
            )

        if identity.relation == "exact" and not partial:
            return EquivalencePrediction(
                "exact", None, ("identity:exact", "all_stated_facets_supported")
            )
        return EquivalencePrediction(
            "acceptable",
            None,
            (
                f"identity:{identity.relation}",
                *(partial or ("all_stated_facets_supported",)),
            ),
        )


def _stated_facets(facets: FoodFacets) -> tuple[str, ...]:
    stated = [
        name
        for name in _SCALAR_FACETS
        if getattr(facets, name) not in (None, "unknown")
    ]
    if facets.preservation_methods:
        stated.append("preservation_methods")
    if facets.processes - _NON_BLOCKING_PROCESSES:
        stated.append("processes")
    return tuple(stated)
