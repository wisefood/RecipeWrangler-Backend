"""Match ingredient names to FoodOn concepts without discarding food identity.

Reviewed aliases and unambiguous normalized labels precede vector retrieval.
Retrieved candidates and leading-word simplification require shared lexical
identity support. Relaxed retrieval distance does not prove an ontology ancestor.
"""

from __future__ import annotations

import re

from recipe_wrangler.utils.food_identity import compare_identities

from recipe_wrangler.repositories.vector_matchers import query_vector_collection

CLASSIFICATION_VERSION = "foodon-link-v4"
DEFAULT_MAX_DISTANCE = 0.15
DEFAULT_ANCESTOR_MAX_DISTANCE = 0.30

# Reviewed aliases for common recipe concepts whose FoodOn labels use a
# different, but equivalent, formulation. They are identity-preserving and are
# deliberately much narrower than embedding matching.
REVIEWED_EXACT_ALIASES: dict[str, str] = {
    "crabmeat": "FOODON_03301081",
    "crab meat": "FOODON_03301081",
    "nam pla": "FOODON_03317197",
    "nuoc mam": "FOODON_03317197",
    "soy milk": "FOODON_03305289",
    "soya milk": "FOODON_03305289",
    "soymilk": "FOODON_03305289",
    "hot toast": "FOODON_03306255",
    "firm tofu": "FOODON_00005540",
    # `normalize_foodon_label` singularises a trailing "s".
    "mixed vegetable": "FOODON_00002683",
    "gluten free pad thai noodle": "FOODON_00005437",
}

# These preserve a qualified food family, not an exact retail formulation.
_REVIEWED_QUALIFIED_FAMILIES = {
    "sweetened coconut": "FOODON_03316908",
    "vanilla flavored soymilk": "FOODON_03305289",
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
_PLANT_MILK_TERMS = frozenset({
    "almond", "coconut", "oat", "rice", "soy", "soya", "cashew",
    "hazelnut", "hemp", "pea", "nut",
})
_UNCERTAIN_FORMULATION_RE = re.compile(
    r"\b(?:or|without|substitute|alternative)\b|\bmay\s+contain\b|"
    r"\bno(?:[\s-]+added)?[\s-]+(?:milk|eggs?|wheat|gluten|nuts?|sesame|soy)\b|"
    r"\b(?:dairy|milk|egg|wheat|gluten|nuts?|sesame|soy)[\s-]*free\b",
    re.I,
)


def _identity_supported(name: str, label: str) -> bool:
    label = re.sub(r"\s+food product$", "", label.strip(), flags=re.I)
    return compare_identities(name, label).relation in {
        "exact", "synonym", "likely_equivalent"
    }


def supported_existing_link(name: str, foodon_id: str, label: str) -> bool:
    """Recheck old mappings before allowing them to create composition evidence."""
    if re.search(r"\b(?:optional|or|without)\b|\bmay\s+contain\b", name, re.I):
        return False
    if _UNCERTAIN_FORMULATION_RE.search(name):
        alias = match_reviewed_alias(name)
        return bool(alias and alias[1] == "reviewed_exact_alias" and alias[0] == foodon_id)
    if _identity_supported(name, label):
        return True
    alias = match_reviewed_alias(name)
    # Cost-group aliases can discard source identity (e.g. beef tomato → meat).
    return bool(alias and alias[1] in {"reviewed_exact_alias", "reviewed_food_family_alias"}
                and alias[0] == foodon_id)


def ingredient_link_batches(session, *, ingredient_ids=None, recipe_id=None, positive_evidence=False):
    """Read at most 100 ingredients per query, without loading any model."""
    from neo4j import Query, ManagedTransaction, Transaction

    query = """
    MATCH (i:Ingredient)
    WHERE elementId(i) > $after
      AND ($ingredient_ids IS NULL OR elementId(i) IN $ingredient_ids)
      AND ($recipe_id IS NULL OR EXISTS {
        MATCH (:Recipe {recipe_id:$recipe_id})-[:HAS_INGREDIENT]->(i)
      })
    WITH i ORDER BY elementId(i) LIMIT 100
    OPTIONAL MATCH (i)-[:HAS_CLASS]->(f:FoodOnClass)
    RETURN elementId(i) AS element_id, i.name AS name,
           collect({foodon_id:f.foodon_id, label:f.label}) AS classes
    ORDER BY element_id
    """
    after = ""
    while True:
        statement = query if isinstance(session, (ManagedTransaction, Transaction)) else Query(query, timeout=60)
        rows = list(session.run(statement, {"after": after, "ingredient_ids": ingredient_ids, "recipe_id": recipe_id}))
        if not rows:
            return
        next_after = rows[-1]["element_id"]
        if next_after <= after:
            raise RuntimeError("Ingredient link pagination did not advance")
        batch = []
        for row in rows:
            name = row["name"] or ""
            exact_alias = match_reviewed_alias(name) if positive_evidence else None
            batch.append({"element_id": row["element_id"], "name": name,
                          "class_ids": [c["foodon_id"] for c in row["classes"]
                                        if c.get("foodon_id") and c.get("label")
                                        and supported_existing_link(name, c["foodon_id"], c["label"])
                                        and (not positive_evidence or _identity_supported(name, c['label'])
                                             or (exact_alias and exact_alias[1] == 'reviewed_exact_alias'
                                                 and exact_alias[0] == c['foodon_id']))]})
        yield batch
        after = next_after


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


def _unqualified_label(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", name)).strip().casefold()


def normalize_foodon_label(name: str) -> str:
    """Singularize for exact-label matching.

    "-ies" -> "-y" (raspberries -> raspberry) and "-oes" -> "-o"
    (tomatoes -> tomato, not the bare "-s" strip's "tomatoe") both need
    special-casing before the generic trailing-"s" strip.
    """
    n = _unqualified_label(name)
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
    """Index unambiguous food-concept labels in the FOODON namespace."""
    rows = session.run(
        "MATCH (f:FoodOnClass) WHERE f.label IS NOT NULL AND f.foodon_id STARTS WITH 'FOODON_' "
        "RETURN f.foodon_id AS id, f.label AS label"
    )
    index: dict[str, str] = {}
    priority: dict[str, int] = {}
    for r in rows:
        label = r["label"].strip()
        # FoodOn's generic concept suffix adds no ingredient or preparation.
        core = re.sub(r"\s+food product$", "", label, flags=re.I)
        key = normalize_foodon_label(core)
        # Parenthetical food identities must not become a different exact key.
        unqualified = _unqualified_label(core)
        if not _identity_supported(core, unqualified):
            continue
        score = 0 if label.casefold() == unqualified else 1 if core.casefold() == unqualified else 2
        if key not in priority or score < priority[key]:
            index[key] = r["id"]
            priority[key] = score
        elif score == priority[key] and index.get(key) != r["id"]:
            # A literal label beats normalized variants; tied IDs stay unresolved.
            index.pop(key, None)
    return index


def match_exact(name: str, label_index: dict[str, str]) -> str | None:
    key = normalize_foodon_label(name)
    return label_index.get(key) if _identity_supported(name, _unqualified_label(name)) else None


def match_reviewed_alias(name: str) -> tuple[str, str, float, bool] | None:
    """Return a narrowly reviewed FoodOn alias without vector search."""
    normalized_name = normalize_foodon_label(name)
    reviewed_foodon_id = REVIEWED_EXACT_ALIASES.get(normalized_name)
    if reviewed_foodon_id and _identity_supported(name, normalized_name):
        return reviewed_foodon_id, "reviewed_exact_alias", 1.0, False

    words = set(re.findall(r"[a-z]+", name.casefold() + " " + normalized_name))
    if _UNCERTAIN_FORMULATION_RE.search(name):
        return None
    family = _REVIEWED_QUALIFIED_FAMILIES.get(normalized_name.replace('-', ' '))
    if family:
        return family, "reviewed_food_family_alias", 0.95, True
    if words & _PLANT_MILK_TERMS and words & {"milk", "cream", "yogurt", "yoghurt"}:
        return None
    # Avoid classifying alternatives and parser failures simply because they
    # mention a dairy or vegetable word. Simple qualified food names are safe.
    simple_product = len(words) <= 8 and not (words & _COMPOUND_FAMILY_BLOCKERS)
    if simple_product:
        if "milk" in words and not (words & _PLANT_MILK_TERMS):
            return "FOODON_00001256", "reviewed_food_family_alias", 0.95, True
        for term, foodon_id in _REVIEWED_FAMILY_ALIASES:
            if set(term.split()) <= words:
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
        if distance is None or not 0 <= distance <= max_distance:
            continue
        metadata = c.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        label = metadata.get("label") or c.get("document")
        if not isinstance(label, str) or not _identity_supported(name, label):
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
        remainder = " ".join(safe_stripped[1:])
        if _identity_supported(" ".join(safe_stripped), remainder):
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
    """Relax retrieval distance, retaining the same identity gate; not a proven parent."""
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
    if _UNCERTAIN_FORMULATION_RE.search(name):
        return None

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
