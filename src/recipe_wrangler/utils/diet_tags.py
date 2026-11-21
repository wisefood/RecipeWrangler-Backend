"""Dietary request vocabulary and known-conflict rules.

Free-from requests exclude known allergen presence; unresolved evidence remains
visible with response warnings. Detection cannot establish absence or
cross-contact safety, so recipe creation generates no free-from claims.
"""

from __future__ import annotations

from recipe_wrangler.utils.consumer_suitability import SUPPORTED_CONSUMER_GROUPS

ALLERGEN_ABSENCE_RULES: dict[str, list[str]] = {
    "nut_free": ["peanut", "tree_nut"],
    "dairy_free": ["milk"],
    "gluten_free": ["wheat", "gluten"],
}

# vegan/vegetarian are not allergen-absence rules -- they come from
# SUITABILITY_FOR status="suitable" (scripts/neo4j/classify_vegan_vegetarian.py
# for the batch corpus, _classify_recipe_suitability for a new recipe at
# import time). Pescatarian safety is likewise a composition question: fish and
# shellfish are allowed, while meat and poultry block it. It must never be
# inferred from absence of fish allergens (the previous implementation did
# exactly that and therefore answered the opposite question).
#
# gluten_free_option requires explicit adaptation/substitution evidence from
# recipe text; scripts/tag_gluten_free_options.py owns that rule. All seven are
# listed explicitly so this tuple is also the closed Elasticsearch vocabulary.
DIET_TAG_NAMES: tuple[str, ...] = (
    "vegan",
    "vegetarian",
    "pescatarian_safe",
    "nut_free",
    "dairy_free",
    "gluten_free",
    "gluten_free_option",
)


def compute_diet_tags(allergens: set[str]) -> list[str]:
    """Presence detection cannot establish absence or cross-contact safety.

    Keep this import-compatible entry point for recipe creation; explicit
    source claims are retained separately by its callers.
    """
    return []
