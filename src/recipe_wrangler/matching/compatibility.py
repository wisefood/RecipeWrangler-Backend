"""Explainable facet compatibility evidence for experiments only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from recipe_wrangler.matching.models import FoodFacets


CompatibilityRelation = Literal[
    "agree",
    "partial_agreement",
    "conflict",
    "insufficient_information",
    "not_evaluated",
]

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
_HARD_CONTRADICTION_FACETS = frozenset(
    {
        "biological_source",
        "food_part",
        "preparation_state",
        "preservation_methods",
        "sodium_level",
        "fat_level",
        "sugar_level",
        "grain_refinement",
        "skin_status",
        "bone_status",
        "packing_medium",
        "gluten_free",
    }
)


@dataclass(frozen=True)
class FacetCompatibility:
    relations: tuple[tuple[str, CompatibilityRelation], ...]
    hard_contradictions: tuple[str, ...]

    def relation_for(self, facet: str) -> CompatibilityRelation:
        return dict(self.relations)[facet]


def compare_facets(mention: FoodFacets, candidate: FoodFacets) -> FacetCompatibility:
    """Compare explicit facets without making an equivalence or ranking decision."""
    relations: list[tuple[str, CompatibilityRelation]] = [
        ("identity", _identity_relation(mention.identity, candidate.identity))
    ]
    relations.extend(
        (facet, _scalar_relation(getattr(mention, facet), getattr(candidate, facet)))
        for facet in _SCALAR_FACETS
    )
    relations.append(("processes", _set_relation(mention.processes, candidate.processes)))
    relations.append(
        (
            "preservation_methods",
            _preservation_relation(
                mention.preservation_methods,
                candidate.preservation_methods,
            ),
        )
    )
    hard = tuple(
        facet
        for facet, relation in relations
        if relation == "conflict" and facet in _HARD_CONTRADICTION_FACETS
    )
    return FacetCompatibility(tuple(relations), hard)


def _identity_relation(mention: str | None, candidate: str | None) -> CompatibilityRelation:
    if not mention or not candidate:
        return "insufficient_information"
    if " ".join(mention.casefold().split()) == " ".join(candidate.casefold().split()):
        return "agree"
    # Lexically different food labels require a separate identity-equivalence
    # model; treating every difference as a contradiction would be unsafe.
    return "not_evaluated"


def _scalar_relation(mention: object, candidate: object) -> CompatibilityRelation:
    if mention in (None, "unknown") or candidate in (None, "unknown"):
        return "insufficient_information"
    return "agree" if mention == candidate else "conflict"


def _set_relation(
    mention: frozenset[str], candidate: frozenset[str]
) -> CompatibilityRelation:
    if not mention or not candidate:
        return "insufficient_information"
    if mention.issubset(candidate):
        return "agree"
    if mention & candidate:
        return "partial_agreement"
    return "conflict"


def _preservation_relation(
    mention: frozenset[str], candidate: frozenset[str]
) -> CompatibilityRelation:
    """Treat explicit fresh as unpreserved, not as missing information."""
    if mention == frozenset({"fresh"}):
        return "agree" if not candidate or candidate == mention else "conflict"
    return _set_relation(mention, candidate)
