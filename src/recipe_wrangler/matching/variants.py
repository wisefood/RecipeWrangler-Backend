"""Proposed preparation families for review, independent of match equivalence."""

from __future__ import annotations

from dataclasses import dataclass

from recipe_wrangler.matching.models import FoodFacets


@dataclass(frozen=True)
class FoodVariantFamily:
    """A structural grouping key, not an approved nutritional substitution.

    Preparation is deliberately excluded. Species, part and product form are
    retained; an unknown source never merges with an explicitly stated source.
    """

    base_identity: str
    biological_source: str | None
    food_part: str | None
    physical_form: str | None


def variant_family(facets: FoodFacets) -> FoodVariantFamily | None:
    """Group normalized mentions/records without inventing unknown properties."""
    if not facets.base_identity or facets.compound is True:
        return None
    return FoodVariantFamily(
        facets.base_identity, facets.biological_source,
        facets.food_part, facets.physical_form,
    )
