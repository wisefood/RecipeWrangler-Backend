"""The `diet_tags` facet -- single source of truth for allergen-absence rules.

Shared between scripts/facets/tag_diet.py (batch materialization over the
existing corpus) and repositories.neo4j_recipes.upsert_recipe_to_neo4j (same
rules applied to a brand-new recipe at import time), so the two never drift
into disagreeing definitions of what "nut_free" means -- the exact failure
mode tag_diet.py's docstring documents as the reason this facet exists.

Every allergen listed here has a FoodOn root configured in
food_ontology.ALLERGEN_DETECTION_RULES (checked -- see roots for milk, egg,
peanut, tree_nut, wheat, soy, fish, crustacean_shellfish, sesame, gluten,
molluscs), so absence is computed from the full keyword+FoodOn-ancestry
allergen set (neo4j_recipes Step 3 + 3b), not keyword matches alone.
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
    """Derive allergen-absence diet tags from a recipe's complete allergen set."""
    return [
        tag
        for tag, blocking in ALLERGEN_ABSENCE_RULES.items()
        if not allergens.intersection(blocking)
    ]
