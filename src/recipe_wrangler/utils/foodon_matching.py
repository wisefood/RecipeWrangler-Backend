"""Shared Ingredient -> FoodOnClass matching cascade.

Single source of truth for the 4-tier match logic, used by both the batch
corpus rebuild (``scripts/facets/link_foodon_classes.py``) and per-recipe
import (``repositories.neo4j_recipes.upsert_recipe_to_neo4j``), so a
brand-new ingredient introduced by a new recipe gets classified the same
way, at the same confidence, as the rest of the corpus -- instead of
sitting unlinked until the next manual batch rebuild.

Tiers, in order: exact normalized label match, hybrid embedding+lexical
search (strict distance), head-noun stripping (numbers/size-words
unlimited, one more word capped) retried through the first two tiers, then
a loosened-distance embedding retry as a last-resort coarser/ancestor-level
match. See ``scripts/facets/link_foodon_classes.py``'s module docstring for
the reasoning and known failure modes behind each tier.
"""

from __future__ import annotations

import re

from recipe_wrangler.repositories.vector_matchers import query_vector_collection

CLASSIFICATION_VERSION = "foodon-link-v1"
DEFAULT_MAX_DISTANCE = 0.15
DEFAULT_ANCESTOR_MAX_DISTANCE = 0.30

# Reviewed aliases for common recipe concepts whose FoodOn labels use a
# different, but equivalent, formulation. They are identity-preserving and are
# deliberately much narrower than embedding matching.
REVIEWED_EXACT_ALIASES: dict[str, str] = {
    "firm tofu": "FOODON_00005540",
    # `normalize_foodon_label` singularises a trailing "s".
    "mixed vegetable": "FOODON_00002683",
    "gluten free pad thai noodle": "FOODON_00005437",
}

# These are deliberately food-family mappings, rather than assertions about a
# precise retail product. They let obvious variants contribute to a broad
# FoodOn/cost group while the cost linker stays conservative about assigning a
# specific CostProduct price.
_REVIEWED_FAMILY_ALIASES: tuple[tuple[str, str], ...] = (
    ("yogurt", "FOODON_00001014"),
    ("yoghurt", "FOODON_00001014"),
    ("feta", "FOODON_00001256"),
    ("mozzarella", "FOODON_03303578"),
    ("salad green", "FOODON_03310789"),
    ("salad greens", "FOODON_03310789"),
)
_REVIEWED_VEGETABLE_TERMS = frozenset({
    "arugula", "asparagus", "aubergine", "beetroot", "broccoli", "carrot",
    "capsicum", "corn", "eggplant", "fennel", "greens", "jicama", "kale",
    "kumara", "leek", "lettuce", "mushroom", "okra", "pea", "peas",
    "potato", "radish", "rocket", "spinach", "squash", "sweetcorn", "turnip",
    "vegetable", "vegetables", "vege", "veges",
})
_REVIEWED_ECONOMIC_GROUP_TERMS: tuple[tuple[str, frozenset[str]], ...] = (
    ("FOODON_00001256", frozenset({
        "buttermilk", "cheese", "cream", "parmesan", "yogurt", "yoghurt"
    })),
    ("FOODON_00001209", frozenset({"bean", "beans", "chickpea", "chickpeas", "lentil", "lentils"})),
    ("FOODON_00001006", frozenset({"beef", "ham", "lamb", "pork", "steak", "veal"})),
    ("FOODON_00001131", frozenset({"chicken", "turkey"})),
    ("FOODON_00001248", frozenset({"fish", "salmon", "tuna"})),
    ("FOODON_00001046", frozenset({"prawn", "prawns", "shrimp", "seafood"})),
    ("FOODON_03315615", frozenset({
        "apple", "apples", "apricot", "apricots", "banana", "bananas", "berry",
        "berries", "cherry", "cherries", "fruit", "lemon", "lime", "mango",
        "orange", "oranges", "peach", "peaches", "pear", "pears", "blueberry",
        "blueberries", "blackberry", "blackberries", "gooseberry", "gooseberries",
        "pomegranate", "raspberry", "raspberries", "strawberry", "strawberries",
    })),
    ("FOODON_00001172", frozenset({
        "almond", "almonds", "cashew", "cashews", "coconut", "hazelnut",
        "macadamia", "nut", "nuts", "peanut", "peanuts", "pecan", "pecans",
        "pistachio", "seed", "seeds", "walnut", "walnuts"
    })),
    ("FOODON_00001087", frozenset({"oil"})),
    ("FOODON_00001709", frozenset({
        "amaranth", "bread", "breadcrumb", "breadcrumbs", "biscuit", "cornmeal",
        "flour", "noodle", "noodles", "oat", "oats", "pasta", "pita", "popcorn",
        "rice", "teff", "tortilla", "tortillas",
    })),
    ("FOODON_03420108", frozenset({"chocolate", "cocoa", "honey", "sugar", "syrup"})),
)
_COMPOUND_FAMILY_BLOCKERS = frozenset({"or", "with", "dip", "dressing"})
_PLANT_MILK_TERMS = frozenset({"almond", "coconut", "oat", "rice", "soy"})

UNIT_WORDS = {
    "teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp",
    "cup", "cups", "ounce", "ounces", "oz", "pound", "pounds", "lb", "lbs",
    "gram", "grams", "g", "kg", "kilogram", "kilograms", "ml", "milliliter",
    "milliliters", "litre", "litres", "liter", "liters", "pinch", "pinches",
    "dash", "dashes", "handful", "handfuls", "bunch", "bunches", "slice",
    "slices", "piece", "pieces", "portion", "portions", "serving", "servings",
    "sachet", "sachets", "packet", "packets", "tin", "tins", "can", "cans",
    "jar", "jars", "sprig", "sprigs", "stick", "sticks",
    "knob", "knobs", "drop", "drops", "sprinkle", "sprinkles", "sprinkling",
    "spoon", "spoons", "spoonful", "spoonfuls",
}

_SAFE_LEADING_WORDS = {
    "medium", "large", "small", "extra", "rounded", "heaped", "level",
    "scant", "generous", "big", "little",
} | UNIT_WORDS
_NUMERIC_RE = re.compile(r"^[\d/½¼¾⅓⅔⅛.,-]+$")


def normalize_foodon_label(name: str) -> str:
    """Singularize for exact-label matching.

    "-ies" -> "-y" (raspberries -> raspberry) and "-oes" -> "-o"
    (tomatoes -> tomato, not the bare "-s" strip's "tomatoe") both need
    special-casing before the generic trailing-"s" strip.
    """
    n = name.strip().casefold()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n.endswith("ies") and len(n) > 4:
        n = n[:-3] + "y"
    elif n.endswith("oes") and len(n) > 4:
        n = n[:-2]
    elif n.endswith("ches") and len(n) > 5:
        n = n[:-2]
    elif n.endswith("s") and len(n) > 3 and not n.endswith("ss"):
        n = n[:-1]
    return n


def fetch_label_index(session) -> dict[str, str]:
    """normalized label -> foodon_id. Restricted to FOODON_* food-product
    classes (excludes NCBITaxon_* organism taxonomy -- see
    link_foodon_classes.py's docstring for why that distinction matters)."""
    rows = session.run(
        "MATCH (f:FoodOnClass) WHERE f.label IS NOT NULL AND f.foodon_id STARTS WITH 'FOODON_' "
        "RETURN f.foodon_id AS id, f.label AS label"
    )
    index: dict[str, str] = {}
    for r in rows:
        key = normalize_foodon_label(r["label"])
        index.setdefault(key, r["id"])
    return index


def match_exact(name: str, label_index: dict[str, str]) -> str | None:
    return label_index.get(normalize_foodon_label(name))


def match_reviewed_alias(name: str) -> tuple[str, str, float, bool] | None:
    """Return a narrowly reviewed FoodOn alias without vector search."""
    normalized_name = normalize_foodon_label(name)
    reviewed_foodon_id = REVIEWED_EXACT_ALIASES.get(normalized_name)
    if reviewed_foodon_id:
        return reviewed_foodon_id, "reviewed_exact_alias", 1.0, False

    words = set(re.findall(r"[a-z]+", normalized_name))
    # Avoid classifying alternatives and parser failures simply because they
    # mention a dairy or vegetable word. Simple qualified food names are safe.
    simple_product = len(words) <= 8 and not (words & _COMPOUND_FAMILY_BLOCKERS)
    if simple_product:
        if "milk" in words and not (words & _PLANT_MILK_TERMS):
            return "FOODON_00001256", "reviewed_food_family_alias", 0.95, True
        for term, foodon_id in _REVIEWED_FAMILY_ALIASES:
            if term in normalized_name:
                return foodon_id, "reviewed_food_family_alias", 0.95, True
        if words & _REVIEWED_VEGETABLE_TERMS:
            return "FOODON_00001261", "reviewed_broad_group_alias", 0.9, True
        for foodon_id, terms in _REVIEWED_ECONOMIC_GROUP_TERMS:
            if words & terms:
                return foodon_id, "reviewed_broad_group_alias", 0.9, True
    return None


def match_embedding(name: str, *, max_distance: float) -> tuple[str, float] | None:
    candidates = query_vector_collection("foodon_classes", name, 5)
    for c in candidates:
        candidate_id = str(c["id"])
        if not candidate_id.startswith("FOODON_"):
            continue
        if c.get("lexical_rank") is None:
            continue
        distance = c.get("distance")
        if distance is None or distance > max_distance:
            continue
        return candidate_id, float(distance)
    return None


def _strip_safe_leading(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and (
        _NUMERIC_RE.match(words[i]) or words[i].casefold() in _SAFE_LEADING_WORDS
    ):
        i += 1
    return words[i:]


def match_head_noun(
    name: str, label_index: dict[str, str], *, max_distance: float
) -> tuple[str, str, float] | None:
    words = name.strip().split()
    safe_stripped = _strip_safe_leading(words)

    candidates: list[list[str]] = []
    if safe_stripped and safe_stripped != words:
        candidates.append(safe_stripped)
    if len(safe_stripped) >= 2:
        candidates.append(safe_stripped[1:])

    for remainder_words in candidates:
        remainder = " ".join(remainder_words)
        if not remainder:
            continue
        foodon_id = match_exact(remainder, label_index)
        if foodon_id:
            return foodon_id, "head_noun_exact", 1.0
        hit = match_embedding(remainder, max_distance=max_distance)
        if hit:
            foodon_id, distance = hit
            return foodon_id, "head_noun_embedding", round(1.0 - distance, 3)
    return None


def match_ancestor(name: str, *, max_distance: float) -> tuple[str, float] | None:
    """Last-resort fallback: relax the embedding distance threshold instead
    of leaving the ingredient unmatched. See
    scripts/facets/link_foodon_classes.py's match_ancestor docstring."""
    return match_embedding(name, max_distance=max_distance)


def match_ingredient_to_foodon(
    name: str,
    label_index: dict[str, str],
    *,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    ancestor_max_distance: float = DEFAULT_ANCESTOR_MAX_DISTANCE,
) -> tuple[str, str, float, bool] | None:
    """Run the full 4-tier cascade for one ingredient name.

    Returns (foodon_id, method, confidence, approximate) or None if nothing
    cleared even the loosened ancestor-level threshold.
    """
    reviewed_alias = match_reviewed_alias(name)
    if reviewed_alias and reviewed_alias[1] == "reviewed_exact_alias":
        return reviewed_alias

    foodon_id = match_exact(name, label_index)
    if foodon_id:
        return foodon_id, "exact_label", 1.0, False

    if reviewed_alias:
        return reviewed_alias

    hit = match_embedding(name, max_distance=max_distance)
    if hit:
        foodon_id, distance = hit
        return foodon_id, "embedding", round(1.0 - distance, 3), False

    head_hit = match_head_noun(name, label_index, max_distance=max_distance)
    if head_hit:
        foodon_id, method, confidence = head_hit
        return foodon_id, method, confidence, True

    ancestor_hit = match_ancestor(name, max_distance=ancestor_max_distance)
    if ancestor_hit:
        foodon_id, distance = ancestor_hit
        return foodon_id, "ancestor_embedding", round(1.0 - distance, 3), True

    return None


def write_link(
    session, ingredient_id: str, foodon_id: str, *, method: str, confidence: float,
    approximate: bool = False, ancestor_level: bool = False,
) -> None:
    session.run(
        """
        MATCH (i:Ingredient {canonical_id: $ingredient_id})
        MATCH (f:FoodOnClass {foodon_id: $foodon_id})
        MERGE (i)-[rel:HAS_CLASS]->(f)
        SET rel.method = $method,
            rel.confidence = $confidence,
            rel.approximate = $approximate,
            rel.ancestor_level = $ancestor_level,
            rel.classification_version = $version,
            rel.linked_at = datetime()
        """,
        ingredient_id=ingredient_id,
        foodon_id=foodon_id,
        method=method,
        confidence=confidence,
        approximate=approximate,
        ancestor_level=ancestor_level,
        version=CLASSIFICATION_VERSION,
    )
