"""Versioned vegan/vegetarian composition rules shared by graph projections."""

from __future__ import annotations

import re
from typing import Any

SUITABILITY_CLASSIFICATION_VERSION = "vegan-vegetarian-v1"
SUPPORTED_CONSUMER_GROUPS = ("vegan", "vegetarian")

DEFINITION_SOURCES: dict[str, list[str]] = {
    "vegan": [
        "fooddrinkeurope_evu_joint_statement",
        "safe_vegan_standards",
    ],
    "vegetarian": ["fooddrinkeurope_evu_joint_statement"],
}

# Only reviewed, reasonably specific roots are used. The much broader FoodOn
# ``animal food product`` and ``animal lipid food product`` roots are omitted
# because the current ingredient mappings contain material false positives.
DIETARY_ORIGINS: tuple[dict[str, Any], ...] = (
    {
        "name": "animal_meat",
        "roots": [
            "FOODON_00001006",  # mammalian meat food product
            "FOODON_00001131",  # poultry meat food product
            "FOODON_00002201",  # amphibian or reptile meat food product
            "FOODON_00002477",  # game animal food product
            "FOODON_00002165",  # organ meat product
        ],
        "vegan_status": "not_suitable",
        "vegetarian_status": "not_suitable",
    },
    {
        "name": "animal_seafood",
        "roots": [
            "FOODON_00001046",  # animal seafood product
            "FOODON_00001248",  # fish food product
            "FOODON_00001293",  # shellfish food product
        ],
        "vegan_status": "not_suitable",
        "vegetarian_status": "not_suitable",
    },
    {
        "name": "dairy",
        "roots": ["FOODON_00001256"],
        "vegan_status": "not_suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "egg",
        "roots": ["FOODON_00001274"],
        "vegan_status": "not_suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "bee_product",
        "roots": [
            "FOODON_00001218",  # bee food product
            "FOODON_00001178",  # honey food product
        ],
        "vegan_status": "not_suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "animal_derivative",
        "roots": [
            "FOODON_00001900",  # gelatin refined food product
            "FOODON_00001899",  # gelatin dessert food product
            "FOODON_00001237",  # natural animal-derived rennet
        ],
        "vegan_status": "not_suitable",
        "vegetarian_status": "not_suitable",
    },
    {
        "name": "plant",
        "roots": [
            "FOODON_00001015",  # plant food product
            "FOODON_00002129",  # plant-based meat product analog
            "FOODON_00002134",  # plant-based seafood product analog
            "FOODON_00002260",  # soybean-based meat product analog
        ],
        "vegan_status": "suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "fungal",
        "roots": ["FOODON_00001143"],
        "vegan_status": "suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "microbial",
        "roots": ["FOODON_00001145"],
        "vegan_status": "suitable",
        "vegetarian_status": "suitable",
    },
    {
        "name": "mineral_or_water",
        "roots": [
            "FOODON_00002041",  # mineral water food product
            "FOODON_00002221",  # salt product
            "FOODON_00002340",  # water food product
        ],
        "vegan_status": "suitable",
        "vegetarian_status": "suitable",
    },
)

GROUP_RULES: dict[str, dict[str, list[str]]] = {
    "vegan": {
        "blocking_allergens": [
            "milk",
            "egg",
            "fish",
            "crustacean_shellfish",
            "molluscs",
        ],
        "positive_allergens": [],
        "blocking_keywords": [
            "beef",
            "pork",
            "bacon",
            "ham",
            "turkey",
            "chicken",
            "duck",
            "goose",
            "lamb",
            "mutton",
            "veal",
            "venison",
            "goat",
            "meat",
            "sausage",
            "pepperoni",
            "prosciutto",
            "fish",
            "salmon",
            "tuna",
            "cod",
            "shrimp",
            "prawn",
            "crab",
            "lobster",
            "shellfish",
            "oyster",
            "milk",
            "cheese",
            "butter",
            "cream",
            "yogurt",
            "yoghurt",
            "egg",
            "gelatin",
            "collagen",
            "whey",
            "casein",
            "honey",
            "beeswax",
            "propolis",
            "colostrum",
            "lanolin",
            "lard",
            "tallow",
            "suet",
            "animal rennet",
        ],
        "positive_keywords": ["vegan", "plant based", "plant-based"],
    },
    "vegetarian": {
        "blocking_allergens": [
            "fish",
            "crustacean_shellfish",
            "molluscs",
        ],
        "positive_allergens": ["milk", "egg"],
        "blocking_keywords": [
            "beef",
            "pork",
            "bacon",
            "ham",
            "turkey",
            "chicken",
            "duck",
            "goose",
            "lamb",
            "mutton",
            "veal",
            "venison",
            "goat",
            "meat",
            "sausage",
            "pepperoni",
            "prosciutto",
            "fish",
            "salmon",
            "tuna",
            "cod",
            "shrimp",
            "prawn",
            "crab",
            "lobster",
            "shellfish",
            "oyster",
            "gelatin",
            "collagen",
            "lard",
            "tallow",
            "suet",
            "animal rennet",
        ],
        "positive_keywords": [
            "vegan",
            "vegetarian",
            "plant based",
            "plant-based",
            "milk",
            "dairy",
            "cheese",
            "butter",
            "cream",
            "yogurt",
            "yoghurt",
            "egg",
            "whey",
            "casein",
            "honey",
            "beeswax",
            "propolis",
            "colostrum",
            "lanolin",
        ],
    },
}

VEGAN_NAME_EXCLUSIONS = [
    r".*\b(vegan|plant[ -]*based)\b.*",
    r".*\b(coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"([ -]+(flavoured|flavored))?[ -]+(milk|cream|yogurt|yoghurt)\b.*",
    r".*\b(non[ -]*dairy|dairy[ -]*free)\b.*",
    r".*\b(peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter\b.*",
    r".*\b(vegetable|plant[ -]*based)[ -]+(suet|lard)\b.*",
    r".*\bbutter[ -]*beans?\b.*",
    r".*\bbutternut\b.*",
]

VEGETARIAN_NAME_EXCLUSIONS = [
    r".*\b(vegan|vegetarian|plant[ -]*based)\b.*",
    *VEGAN_NAME_EXCLUSIONS[1:],
]

POSITIVE_EVIDENCE_EXCLUSIONS = [
    r"^\s*\*?\s*note\b.*",
    r".*\bingredients? with an asterisk\b.*",
    r".*\bcheck the label\b.*",
]


def keyword_regex(keyword: str) -> str:
    """Return a case-insensitive-ready Neo4j/Python word-boundary pattern."""

    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"[\s-]+")
    return rf".*\b{escaped}(e?s)?\b.*"


def origin_rows() -> list[dict[str, Any]]:
    """Return mutable dictionaries suitable for Neo4j parameters."""

    return [
        {
            "name": str(origin["name"]),
            "roots": list(origin["roots"]),
            "vegan_status": str(origin["vegan_status"]),
            "vegetarian_status": str(origin["vegetarian_status"]),
        }
        for origin in DIETARY_ORIGINS
    ]
