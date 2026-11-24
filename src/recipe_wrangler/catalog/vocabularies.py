"""Controlled vocabularies for recipe annotation.

Annotation is only as good as the vocabulary behind it. Left unconstrained, a
model will label one recipe ``italian``, the next ``Italian cuisine`` and a
third ``mediterranean/italian``, and the facet becomes unusable for browsing.
Everything here exists to make the label set closed and stable.

Each facet is sourced as strongly as the data allows, and the difference is
recorded rather than papered over:

``food_groups``
    **Derived from the graph.** Every ingredient's FoodOn class ancestry is
    already denormalized onto the recipe as ``ingredient_class_ancestors``, so
    food groups are a deterministic lookup — no model involved. FoodOn's upper
    ontology has to be filtered out first: "anatomical entity", "material
    entity" and "food material" are true of nearly everything and describe
    nothing.

``flavor_profiles``
    **Model-assigned against a fixed list.** FlavorDB is not usable here: it
    stores *molecules* (``(+)-3-Carene``, ``Linalool``), which mean nothing to a
    diner and carry no mapping to taste. Deriving "sweet" from "vanillin" would
    be an invention dressed up as data, so the sensory vocabulary is declared
    below and assignment is openly a model judgement. (FlavorDB's ``PAIRS_WITH``
    graph is genuinely useful — for ingredient pairing and substitution, which
    is ingredient-level work, not recipe annotation.)

``cuisines`` / ``moods``
    **Model-assigned against a fixed list.** Nothing in any store carries
    either. ``moods`` is additionally subjective, which is why it is kept
    small, concrete and occasion-based rather than emotional.

Only ``food_groups`` may be written to a human-authoritative field, because it
is the only facet actually derived from data. The rest land in ``ai_*`` with an
``annotation_evidence`` entry recording method and confidence.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

CLASSIFICATION_VERSION = "annotation-v1"

# --------------------------------------------------------------------------- #
# Food groups — derived from FoodOn
# --------------------------------------------------------------------------- #

# FoodOn ancestors that are ontologically true but semantically empty: they
# apply to almost every ingredient and would put every recipe in every group.
FOODON_UPPER_ONTOLOGY: frozenset[str] = frozenset(
    {
        "anatomical entity",
        "material entity",
        "food material",
        "food product",
        "food material component",
        "food material by process",
        "plant structure",
        "plant material",
        "multi-tissue plant structure",
        "multi-component food",
        "processed food product",
        "food",
    }
)

# FoodOn class ID -> the food group we publish.
#
# Keyed by ID, not label. An earlier draft mapped labels and silently matched
# nothing, because `ingredient_class_ancestors` stores IDs; worse, the labels
# were guessed rather than looked up, and 10 of 27 did not exist in this
# ontology at all — including every spelling of "meat". These IDs were read back
# out of the deployed graph (see scripts/catalog/inspect_foodon.py), restricted
# to classes actually reachable from our ingredients.
#
# Several IDs map to one group on purpose: FoodOn separates "mammalian meat
# food product" from "processed meat food product" from "pork meat food
# product", and for browsing they are all meat.
FOODON_ID_TO_FOOD_GROUP: dict[str, str] = {
    # vegetables & fruit
    "FOODON_00001261": "vegetables",
    "FOODON_00002153": "vegetables",   # plant seed vegetable food product
    # Prepared mixed vegetables live under FoodOn's parallel multi-component
    # branch and never reach ``vegetable food product`` through SUBCLASS_OF.
    "FOODON_00002683": "vegetables",   # mixed vegetables
    "FOODON_00001057": "fruit",        # plant fruit food product
    # grains
    "FOODON_00001093": "grains",       # cereal grain food product
    "FOODON_00001173": "grains",       # plant seed food product
    "FOODON_00002151": "grains",       # plant seed based bakery food product
    # Pasta is in FoodOn's processed-food branch rather than below cereal food.
    "FOODON_00001211": "grains",       # pasta food product
    # nuts & seeds
    "FOODON_00001172": "nuts_and_seeds",
    "FOODON_03460177": "nuts_and_seeds",  # plant seed or nut food product
    # dairy
    "FOODON_00001256": "dairy",        # dairy food product
    "FOODON_00001257": "dairy",        # milk or milk based food product
    "FOODON_00001771": "dairy",        # cow milk based food product
    "FOODON_00001013": "dairy",        # cheese food product
    "FOODON_00001127": "dairy",        # cow milk cheese
    # meat & poultry
    "FOODON_00001006": "meat",         # mammalian meat food product
    "FOODON_00001010": "meat",         # processed meat food product
    "FOODON_00001007": "meat",         # meat sausage food product
    "FOODON_00001038": "meat",         # pork meat food product
    "FOODON_00001134": "meat",         # bovine meat food product
    "FOODON_02010107": "meat",         # piece of animal meat
    "FOODON_00001283": "poultry",
    "FOODON_02020686": "poultry",      # poultry material
    # Duck never reaches "poultry material" through SUBCLASS_OF -- FoodOn's
    # ancestry for it stops at "duck material", a sibling branch, not a
    # descendant of the poultry-material node the rest of this map relies on.
    "FOODON_02020115": "poultry",      # duck material
    # fish & seafood
    "FOODON_00001248": "fish",         # fish food product
    "FOODON_03411365": "fish",         # bony fish
    "FOODON_00005674": "fish",         # bony fish material
    "FOODON_00001293": "seafood",      # shellfish food product
    # other
    "FOODON_00001274": "eggs",
    # Plain "egg"/"eggs" ingredient entries classify to this node directly,
    # never reaching FOODON_00001274 -- a separate branch, same gap shape as
    # duck above.
    "FOODON_03420225": "eggs",         # egg (without shell)
    # egg white / egg yolk resolve through "poultry material" too (both are
    # animal substance under avian material), so without their own more
    # specific entries here they were counted as poultry, not eggs. Adding
    # these adds "eggs" alongside "poultry" for those two ingredients rather
    # than replacing it -- this map has no notion of "most specific wins".
    "FOODON_03304840": "eggs",         # chicken egg white food product
    "FOODON_03315772": "eggs",         # chicken egg yolk food product
    "FOODON_00001264": "legumes",
    "FOODON_00001209": "legumes",      # pulse food product
    "FOODON_00003042": "herbs_and_spices",
    "FOODON_03303380": "herbs_and_spices",
    "FOODON_00001149": "sugars",       # confectionery food
}

# Note the absence of a `beverages` food group. FoodOn's beverage classes match
# *ingredients* (stock, wine, juice), so a stew made with stock would land in
# it — and it would collide with the `beverages` course type, which means the
# dish itself is a drink. One name, two meanings, is the exact conflation that
# made the existing category UI unusable. Browsing drinks is a course-type
# question; this facet answers "what food groups does it contain".
FOOD_GROUPS: tuple[str, ...] = tuple(
    dict.fromkeys(FOODON_ID_TO_FOOD_GROUP.values())
)


def _normalize_foodon_id(value: object) -> str:
    """FoodOn IDs appear lower-cased in the index and upper-cased in the graph."""
    return str(value or "").strip().upper().replace(":", "_")


def food_groups_from_foodon(ancestor_ids: Iterable[str]) -> list[str]:
    """Map FoodOn ancestor class IDs onto published food groups.

    Deterministic: no model, no confidence score. An unmapped ancestor is
    dropped rather than guessed at.
    """
    groups: list[str] = []
    for raw in ancestor_ids or ():
        group = FOODON_ID_TO_FOOD_GROUP.get(_normalize_foodon_id(raw))
        if group and group not in groups:
            groups.append(group)
    return sorted(groups)


_CULINARY_VEGETABLE_TERMS = (
    "asparagus", "aubergine", "beetroot", "broccoli", "cabbage", "capsicum",
    "carrot", "cauliflower", "celery", "chard", "chilli", "courgette",
    "cucumber", "eggplant", "fennel", "kale", "leek", "lettuce", "mushroom",
    "okra", "onion", "pepper", "potato", "pumpkin", "radish", "spinach",
    "squash", "tomato", "zucchini",
)

_CULINARY_FRUIT_TERMS = (
    "apple", "apricot", "avocado", "banana", "berries", "berry",
    "blackberry", "blueberry", "boysenberry", "cherry", "citrus", "cranberry",
    "date", "feijoa", "fig", "goji", "grape", "kiwi", "kiwifruit", "lemon",
    "lime", "mango", "melon", "nectarine", "orange", "passionfruit", "peach",
    "pear", "pineapple", "plum", "raisin", "raspberry", "strawberry",
)

_CULINARY_HERB_TERMS = (
    "basil", "chive", "cilantro", "coriander", "dill", "mint", "oregano",
    "parsley", "rosemary", "sage", "tarragon", "thyme",
)

_MEAT_TERMS = (
    "bacon", "beef", "boar", "chorizo", "deer", "frankfurter", "goat", "ham",
    "kolbász", "lamb", "liver", "meat", "meatball", "mince", "mutton", "offal",
    "pork", "prosciutto", "rabbit", "salami", "sausage", "steak", "veal",
    "venison",
)

_SEAFOOD_TERMS = (
    "calamari", "clam", "crab", "crayfish", "langoustine", "lobster", "mussel",
    "octopus", "oyster", "prawn", "scallop", "seafood", "shellfish", "shrimp",
    "squid",
)


def _contains_food_term(text: str, terms: Iterable[str]) -> bool:
    """Match whole ingredient words, allowing ordinary English plurals."""
    for term in terms:
        escaped = re.escape(term[:-1]) + r"(?:y|ies)" if term.endswith("y") else re.escape(term) + r"(?:s|es)?"
        if re.search(rf"(?<!\w){escaped}(?!\w)", text):
            return True
    return False


def _resolve_ingredient_groups(name: str, distances: dict[str, int]) -> set[str]:
    """Select consumer-facing groups for one ingredient from FoodOn ancestry."""
    candidates = set(distances)
    lowered = str(name or "").casefold()
    named_vegetable = _contains_food_term(lowered, _CULINARY_VEGETABLE_TERMS)
    if (
        "vegetables" in candidates
        and not named_vegetable
        and _contains_food_term(lowered, _CULINARY_FRUIT_TERMS)
    ):
        # Some fruit ingredients are linked directly to FoodOn's broad
        # "mixed vegetables" node, so there is no fruit ancestor to win.
        candidates.discard("vegetables")
        candidates.add("fruit")
        distances.setdefault("fruit", distances["vegetables"])
    named_fruit = _contains_food_term(lowered, _CULINARY_FRUIT_TERMS)
    if {"fruit", "vegetables"} <= candidates and (named_vegetable or named_fruit):
        produce = "vegetables" if named_vegetable else "fruit"
        candidates -= {"fruit", "vegetables"}
        candidates.add(produce)
    if "vegetables" in candidates and _contains_food_term(
        lowered, _CULINARY_HERB_TERMS
    ):
        candidates.discard("vegetables")
        candidates.add("herbs_and_spices")
        distances.setdefault("herbs_and_spices", distances["vegetables"])

    # Do not choose one globally nearest group. FoodOn's paths describe several
    # independent culinary facts: cheese can legitimately be both dairy and a
    # processed food, while chicken's generic meat ancestor is closer than its
    # poultry ancestor. Resolve only known mutually-conflicting branches.
    selected = set(candidates)
    if "eggs" in selected:
        selected -= {"poultry", "meat"}
    if {"poultry", "meat"} <= selected:
        poultry_terms = ("chicken", "turkey", "duck", "goose", "poultry")
        if _contains_food_term(lowered, poultry_terms):
            selected.discard("meat")
    return selected


def food_groups_from_ingredient_paths(
    paths: Iterable[dict[str, object]],
) -> tuple[list[str], dict[str, dict[str, set[str]]]]:
    """Resolve FoodOn groups per ingredient before combining at recipe level."""
    ingredients: dict[str, dict[str, object]] = {}
    for path in paths or ():
        if not isinstance(path, dict):
            continue
        foodon_id = _normalize_foodon_id(path.get("foodon_id"))
        group = FOODON_ID_TO_FOOD_GROUP.get(foodon_id)
        if not group:
            continue
        key = str(path.get("ingredient_id") or path.get("ingredient") or "").strip()
        if not key:
            continue
        entry = ingredients.setdefault(
            key,
            {"name": str(path.get("ingredient") or key), "distances": {}, "ids": {}},
        )
        distance = int(path.get("distance") or 0)
        distances = entry["distances"]
        ids = entry["ids"]
        distances[group] = min(distance, distances.get(group, distance))
        ids.setdefault(group, set()).add(foodon_id)

    support: dict[str, dict[str, set[str]]] = {}
    for entry in ingredients.values():
        name = str(entry["name"])
        selected = _resolve_ingredient_groups(name, entry["distances"])
        for group in selected:
            group_support = support.setdefault(group, {"foodon_ids": set(), "ingredients": set()})
            ids = entry["ids"].get(group, set())
            if not ids:
                ids = set().union(*entry["ids"].values())
            group_support["foodon_ids"].update(ids)
            group_support["ingredients"].add(name)
    return sorted(support), support


def rejected_food_groups_from_ingredient_paths(
    paths: Iterable[dict[str, object]],
    ingredient_names: Iterable[str] = (),
) -> set[str]:
    """Groups conclusively invalidated by per-ingredient conflict resolution.

    A group is rejected only when at least one ingredient produced it as a
    broad FoodOn ancestor and no ingredient independently retained it. This is
    deliberately narrower than treating missing ancestry as negative evidence:
    parts of the imported FoodOn graph are disconnected, so absence cannot
    safely erase a previously supported group.
    """
    ingredients: dict[str, dict[str, object]] = {}
    for path in paths or ():
        if not isinstance(path, dict):
            continue
        foodon_id = _normalize_foodon_id(path.get("foodon_id"))
        group = FOODON_ID_TO_FOOD_GROUP.get(foodon_id)
        if not group:
            continue
        key = str(path.get("ingredient_id") or path.get("ingredient") or "").strip()
        if not key:
            continue
        entry = ingredients.setdefault(
            key, {"name": str(path.get("ingredient") or key), "distances": {}},
        )
        distance = int(path.get("distance") or 0)
        distances = entry["distances"]
        distances[group] = min(distance, distances.get(group, distance))

    selected_all: set[str] = set()
    rejected: set[str] = set()
    classified_names: list[str] = []
    for entry in ingredients.values():
        classified_names.append(str(entry["name"]).casefold())
        candidates = set(entry["distances"])
        selected = _resolve_ingredient_groups(str(entry["name"]), entry["distances"])
        selected_all.update(selected)
        rejected.update(candidates - selected)
    rejected -= selected_all

    # Historical projections could contain poultry solely because egg white
    # and yolk once inherited through FoodOn's avian-material branch. That
    # stale value is not visible in today's paths, so absence alone cannot
    # identify it. Ingredient identity can: eggs with no poultry ingredient
    # are conclusive evidence that `poultry` is the old ancestry leak.
    all_names = [str(name).casefold() for name in ingredient_names] or classified_names
    has_egg = any(_contains_food_term(name, ("egg",)) for name in all_names)
    poultry_terms = ("chicken", "turkey", "duck", "goose", "poultry")
    has_poultry = any(
        _contains_food_term(name, poultry_terms) for name in all_names
    )
    if has_egg and not has_poultry:
        rejected.add("poultry")
    has_named_vegetable = any(
        _contains_food_term(name, _CULINARY_VEGETABLE_TERMS) for name in all_names
    )
    has_named_fruit = any(
        _contains_food_term(name, _CULINARY_FRUIT_TERMS) for name in all_names
    )
    has_named_herb = any(
        _contains_food_term(name, _CULINARY_HERB_TERMS) for name in all_names
    )
    if has_named_vegetable:
        rejected.discard("vegetables")
    elif has_named_fruit or has_named_herb:
        # Also repairs stale projections whose bad vegetable branch is no
        # longer present in the current graph paths (for example goji berries).
        rejected.add("vegetables")
    if has_named_fruit:
        rejected.discard("fruit")
    if any(_contains_food_term(name, _MEAT_TERMS) for name in all_names):
        rejected.discard("meat")
    elif has_egg:
        rejected.add("meat")
    if any(_contains_food_term(name, _SEAFOOD_TERMS) for name in all_names):
        rejected.discard("seafood")
    elif has_egg:
        rejected.add("seafood")
    return rejected


# --------------------------------------------------------------------------- #
# Model-assigned vocabularies
# --------------------------------------------------------------------------- #

# The basic tastes plus the aroma/mouthfeel descriptors a cook would actually
# use. Deliberately short: a long list produces inconsistent labelling.
FLAVOR_PROFILES: tuple[str, ...] = (
    "sweet",
    "sour",
    "salty",
    "bitter",
    "umami",
    "spicy",
    "smoky",
    "herbaceous",
    "citrusy",
    "nutty",
    "creamy",
    "earthy",
)

# Cuisines that actually occur in a European-facing corpus. Regional rather
# than national where the cooking is genuinely shared.
CUISINES: tuple[str, ...] = (
    "british",
    "irish",
    "french",
    "italian",
    "spanish",
    "portuguese",
    "greek",
    "mediterranean",
    "hungarian",
    "slovenian",
    "balkan",
    "german",
    "nordic",
    "polish",
    "turkish",
    "middle_eastern",
    "north_african",
    "west_african",
    "indian",
    "south_asian",
    "chinese",
    "japanese",
    "korean",
    "thai",
    "vietnamese",
    "southeast_asian",
    "mexican",
    "caribbean",
    "latin_american",
    "american",
)

# Eating occasions rather than emotions: an occasion is observable from the
# recipe, a feeling is not.
MOODS: tuple[str, ...] = (
    "comfort",
    "light",
    "hearty",
    "fresh",
    "indulgent",
    "quick",
    "festive",
    "warming",
    "refreshing",
)

# Sources whose geography is known, so a cuisine prior exists without asking a
# model. Used as a hint in the prompt, never as the answer.
# Claim tags worth planning against, with corpus counts at the time of writing
# (n=4500). `tags` is a human-authored open field — anything can be written to
# it — so this is a curated PLANNING subset, not the full set, and it exists so
# an agent can be told which claims are worth asking for instead of guessing.
#
# A tag no recipe carries is not a narrow filter, it is an empty one: the agent
# surfaces AND their filters, so asking for a claim that does not exist empties
# every slot and the member is told no meals exist. Publishing the list is what
# lets a caller avoid that.
PLANNING_TAGS: tuple[str, ...] = (
    "healthy_and_nutritious", # 2535
    "high_protein",           # 1676
    "low_fat",                # 1081
    "5_ingredients_or_less",  # 563
    "high_fibre",             # 457
    "low_calorie",            # 184
)


# Keyed on the raw `source` value. Offered to the annotator as a prior, never
# an answer, so a source only belongs here when its corpus is *actually*
# dominated by one cuisine. Percentages are the share of that source's already
# annotated recipes carrying the cuisine, measured against the corpus:
SOURCE_CUISINE_PRIOR: dict[str, str] = {
    "Curated Irish Recipes": "irish",           # 266/334, 79%
    "Curated Hungarian Recipes": "hungarian",   # 121/149, 81%
    "Curated Slovenian Recipes": "slovenian",   #  97/100, 97%
    "Slovenian Kitchen": "slovenian",           # 115/123, 93%
    "Best of Hungary": "hungarian",             #  59/74,  80%
    "The Hungary Soul": "hungarian",            #  23/25,  92%
}

# Deliberately absent, despite their names reading as national:
#
# "Irish Heart Foundation" — 7/117 (6%) of its recipes are annotated `irish`.
#   Its top cuisines are italian (16), british (11), american (9) and
#   mediterranean (9). It is a health charity publishing international recipes,
#   so an `irish` prior would push the model toward the wrong answer on 94% of
#   them.
# "SuperValu" — a retailer, with no dominant cuisine at all: mediterranean
#   leads on 11/68 (16%) and `irish` does not reach its top six.
#
# Both are the mistake the annotation prompt warns about in the ingredient
# case — inferring a cuisine from one suggestive signal, here the brand name.

FACETS: dict[str, dict[str, Any]] = {
    "food_groups": {
        "values": FOOD_GROUPS,
        "method": "foodon_derived",
        "authoritative": True,
        "multi": True,
        "description": "Food groups present, from each ingredient's FoodOn class ancestry.",
    },
    "cuisines": {
        "values": CUISINES,
        "method": "model",
        "authoritative": False,
        "multi": True,
        "description": "Culinary tradition the dish belongs to. Omit if not clearly identifiable.",
    },
    "flavor_profiles": {
        "values": FLAVOR_PROFILES,
        "method": "model",
        "authoritative": False,
        "multi": True,
        "description": "Dominant tastes and aromas a diner would notice.",
    },
    "moods": {
        "values": MOODS,
        "method": "model",
        "authoritative": False,
        "multi": True,
        "description": "Eating occasion the dish suits.",
    },
}


def validate_values(facet: str, values: Iterable[str]) -> list[str]:
    """Keep only values in the facet's controlled vocabulary.

    This is the guard that makes the vocabulary closed: anything a model
    returns that is not in the list is discarded, not coerced.
    """
    allowed = set(FACETS[facet]["values"])
    out: list[str] = []
    for value in values or ():
        item = str(value or "").strip().lower().replace(" ", "_")
        if item in allowed and item not in out:
            out.append(item)
    return out


def evidence_entry(
    facet: str,
    value: str,
    *,
    method: str,
    confidence: float | None = None,
    sources: Iterable[str] = (),
    foodon_ids: Iterable[str] = (),
    supporting_ingredients: Iterable[str] = (),
) -> dict[str, Any]:
    """One ``annotation_evidence`` record."""
    return {
        "facet": facet,
        "value": value,
        "status": "assigned",
        "method": method,
        "evidence_status": "derived" if method != "model" else "inferred",
        "confidence": confidence,
        "sources": list(sources),
        "foodon_ids": list(foodon_ids),
        "supporting_ingredients": list(supporting_ingredients),
        "classification_version": CLASSIFICATION_VERSION,
    }
