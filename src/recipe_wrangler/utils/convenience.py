"""The `convenience` facet -- rules and canonical tag names.

Deterministic, computed straight from fields the ES catalog document already
carries (`duration`, `ingredient_count`) -- no Neo4j round-trip needed, unlike
diet_tags/nutrition_claims which start from graph-only facts (allergens,
FoodOn ancestry). Single source of truth shared between
scripts/facets/tag_convenience.py (materializes it across the corpus) and any
future live-write path, so both apply the same thresholds.

Guards against placeholder zeros: a recipe with duration=0 means "unknown",
not "instant" -- 58 recipes in the corpus have this. Rules require > 0.
"""

from __future__ import annotations

CONVENIENCE_TAG_NAMES: tuple[str, ...] = (
    "quick",
    "simple",
)


def compute_convenience_tags(duration: float | None, ingredient_count: int | None) -> list[str]:
    tags: list[str] = []
    if duration is not None and 0 < duration <= 30:
        tags.append("quick")
    if ingredient_count is not None and 0 < ingredient_count <= 5:
        tags.append("simple")
    return tags
