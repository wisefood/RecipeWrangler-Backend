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
    "FOODON_00001057": "fruit",        # plant fruit food product
    # grains
    "FOODON_00001093": "grains",       # cereal grain food product
    "FOODON_00001173": "grains",       # plant seed food product
    "FOODON_00002151": "grains",       # plant seed based bakery food product
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
    # fish & seafood
    "FOODON_00001248": "fish",         # fish food product
    "FOODON_03411365": "fish",         # bony fish
    "FOODON_00005674": "fish",         # bony fish material
    "FOODON_00001293": "seafood",      # shellfish food product
    # other
    "FOODON_00001274": "eggs",
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
    "30_minutes_or_less",     # 2809
    "healthy_and_nutritious", # 2535
    "high_protein",           # 1676
    "low_fat",                # 1081
    "5_ingredients_or_less",  # 563
    "high_fibre",             # 457
    "low_calorie",            # 184
)


SOURCE_CUISINE_PRIOR: dict[str, str] = {
    "Curated Irish Recipes": "irish",
    "Curated Hungarian Recipes": "hungarian",
    "Curated Slovenian Recipes": "slovenian",
}

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
