"""Canonical FATO/FoodOn identifiers and conservative suitability aggregation.

FATO provides the declaration and consumer-group vocabulary. FoodOn provides
the food taxonomy and label-claim identifiers. Neither ontology makes missing
knowledge equivalent to a negative fact, so suitability is deliberately
three-state: suitable, not_suitable, or unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CLASSIFICATION_VERSION = "fato-foodon-v1"
FATO_NAMESPACE = "http://www.w3id.org/FATO/"
FATO_ALLERGEN_CLASS_IRI = f"{FATO_NAMESPACE}Allergen"
FATO_ALLERGEN_DECLARATION_CLASS_IRI = (
    f"{FATO_NAMESPACE}AllergenDeclaration"
)


@dataclass(frozen=True)
class AllergenOntologyMapping:
    """Crosswalk for one internal allergen identifier."""

    foodon_label_claim_id: str
    eu_label: str


# FoodOn's label-claim branch follows the EU declaration groups. ``wheat`` is
# retained as an internal compatibility identifier, but maps to the broader
# cereals-containing-gluten claim instead of pretending it is a separate EU
# declaration group.
ALLERGEN_ONTOLOGY_MAPPINGS: dict[str, AllergenOntologyMapping] = {
    "gluten": AllergenOntologyMapping(
        "FOODON_03510214", "cereals containing gluten"
    ),
    "wheat": AllergenOntologyMapping(
        "FOODON_03510214", "cereals containing gluten"
    ),
    "crustacean_shellfish": AllergenOntologyMapping(
        "FOODON_03510215", "crustaceans"
    ),
    "egg": AllergenOntologyMapping("FOODON_03510216", "eggs"),
    "fish": AllergenOntologyMapping("FOODON_03510217", "fish"),
    "peanut": AllergenOntologyMapping("FOODON_03510218", "peanuts"),
    "soy": AllergenOntologyMapping("FOODON_03510219", "soybeans"),
    "milk": AllergenOntologyMapping("FOODON_03510220", "milk"),
    "tree_nut": AllergenOntologyMapping("FOODON_03510221", "nuts"),
    "celery": AllergenOntologyMapping("FOODON_03510222", "celery"),
    "mustard": AllergenOntologyMapping("FOODON_03510223", "mustard"),
    "sesame": AllergenOntologyMapping("FOODON_03510224", "sesame seeds"),
    "sulphites": AllergenOntologyMapping(
        "FOODON_03510225", "sulphur dioxide and sulphites"
    ),
    "lupin": AllergenOntologyMapping("FOODON_03510228", "lupin"),
    "molluscs": AllergenOntologyMapping("FOODON_03510232", "molluscs"),
}


# Named dairy/meat products that carry no generic allergen/meat word in
# their name (a real gap: "quark" has no FoodOn match AND no keyword hit,
# so it fell through both detection paths to "unknown" -- see quark/mexican
# cheese blend investigation). Shared across the allergen keyword list and
# the vegan/vegetarian blocking/positive keyword lists in
# consumer_suitability.py so a name like "feta" is caught consistently
# everywhere, not just wherever someone happened to add it first.
DAIRY_PRODUCT_KEYWORDS: list[str] = [
    "quark", "halloumi", "feta", "ricotta", "paneer", "mascarpone",
    "mozzarella", "buttermilk", "creme fraiche", "crème fraîche", "labneh",
    "burrata", "brie", "camembert", "cheddar", "gouda", "parmesan",
    "parmigiano", "provolone", "gruyere", "gruyère", "edam", "stilton",
    "roquefort",
]

MEAT_PRODUCT_KEYWORDS: list[str] = [
    "chorizo", "pancetta", "salami", "pastrami", "rashers", "rasher",
    "black pudding", "foie gras", "pate", "pâté", "gammon", "jerky",
    "biltong", "brisket", "sirloin", "offal", "liver", "kidney", "tripe",
]

# FoodOn roots + keyword fallback per allergen. Shared by
# scripts/neo4j/tag_allergens.py (corpus-wide backfill) and
# repositories/neo4j_recipes.py (per-recipe detection at creation time) so
# the two paths detect exactly the same things instead of silently drifting
# apart the way the batch job and the API allergen list once did.
ALLERGEN_DETECTION_RULES: dict[str, dict[str, list[str]]] = {
    "milk": {
        "roots": [
            "FOODON_00001257",  # milk or milk based food product
            "FOODON_00001256",  # dairy food product
            "FOODON_00001771",  # cow milk based food product
            "FOODON_00001118",  # cattle dairy food product
        ],
        "keywords": [
            "milk", "cheese", "butter", "cream", "yogurt", "whey", "casein",
            "lactose", "ghee", "curd", "kefir", *DAIRY_PRODUCT_KEYWORDS,
        ],
    },
    "egg": {
        "roots": [
            "FOODON_00001274",  # egg food product
            "FOODON_00001105",  # avian egg food product
            "FOODON_02010002",  # animal egg
        ],
        "keywords": [
            "egg", "egg white", "egg yolk", "omelet", "mayonnaise", "aioli",
            "meringue", "albumen",
        ],
    },
    "peanut": {
        "roots": [
            "FOODON_00002099",  # peanut food product
            "FOODON_00003206",  # peanut
            "FOODON_00002098",  # peanut fat or oil refined food product
            "FOODON_00005586",  # peanut flour
        ],
        "keywords": [
            "peanut", "peanut butter", "groundnut", "arachis",
            # Satay/saté sauce is peanut-based; recipes often name the dish
            # without ever listing "peanut" as an ingredient.
            "satay", "sate", "saté", "mole sauce", "mole paste",
        ],
    },
    "tree_nut": {
        "roots": [
            "FOODON_00001587",  # almond food product
            "FOODON_00002338",  # walnut food product
            "FOODON_00002107",  # pecan nut food product
            "FOODON_00001688",  # cashew nut food product
            "FOODON_00003690",  # pistachio nut food product
        ],
        "keywords": [
            "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut",
            "macadamia", "brazil nut", "pine nut", "romesco", "praline",
        ],
    },
    "wheat": {
        "roots": [
            "FOODON_00001141",  # wheat food product
            "FOODON_00001210",  # wheat flour food product
            "FOODON_00002347",  # wheat based bakery food product
            "FOODON_00002349",  # wheat based gravy or sauce food product
            "FOODON_00002351",  # wheat bread food product
            "FOODON_00002354",  # wheat pasta
            "FOODON_00001825",  # durum wheat food product
        ],
        "keywords": [
            "wheat", "whole wheat", "durum", "semolina", "farina", "graham",
            "spelt", "bulgur", "couscous", "seitan", "gluten", "flour",
            "bread", "breadcrumbs", "breading", "batter", "roux", "pasta",
            "noodle",
        ],
    },
    "soy": {
        "roots": [
            "FOODON_00002266",  # soybean food product
            "FOODON_00001078",  # fermented soybean food product
            "FOODON_00001235",  # soy sauce food product
            "FOODON_03302389",  # soybean beverage
            "FOODON_03302776",  # soybean oil
            "FOODON_03310553",  # soy protein isolate
            "FOODON_03310368",  # soy protein
            "FOODON_03306653",  # soy lecithin spread
            "FOODON_03305289",  # soybean milk
            "FOODON_03310002",  # soybean paste
        ],
        "keywords": [
            "soy", "soya", "soybean", "edamame", "tofu", "tempeh", "miso",
            "soy sauce", "tamari", "shoyu", "soy lecithin", "lecithin (soy)",
            "textured vegetable protein", "tvp", "soy protein", "soy isolate",
            "soy flour", "soy oil", "soy milk", "soy yogurt", "natto",
        ],
    },
    "fish": {
        "roots": [
            "FOODON_00001248",  # fish food product
            "FOODON_00001055",  # sea water fish food product
            "FOODON_00001249",  # freshwater fish food product
            "FOODON_03315173",  # fish product (unspecified species)
            "FOODON_00001661",  # bony fish food product
            "FOODON_00001054",  # fermented fish or seafood food product
            "FOODON_03317197",  # fish sauce
        ],
        "keywords": [
            "fish", "cod", "bass", "flounder", "salmon", "tuna", "haddock",
            "tilapia", "anchovy", "sardine", "trout", "mackerel", "halibut",
            "pollock", "catfish", "swordfish", "fish sauce",
            "worcestershire sauce", "worcester sauce", "caesar dressing",
        ],
    },
    "crustacean_shellfish": {
        "roots": [
            "FOODON_00001792",  # crustacean food product
            "FOODON_02021444",  # crab food product
            "FOODON_00002007",  # lobster food product
            "FOODON_00002239",  # shrimp food product
        ],
        "keywords": [
            "crab", "lobster", "shrimp", "prawn", "crustacean", "langostino",
        ],
    },
    "sesame": {
        "roots": [
            "FOODON_00002232",  # sesame food product
            "FOODON_03310306",  # sesame seed
            "FOODON_03304152",  # sesame oil
            "FOODON_00004525",  # sesame butter
            "FOODON_00005500",  # sesame flour
            "FOODON_03304154",  # sesame seed paste
        ],
        "keywords": [
            "sesame", "tahini", "sesame oil", "sesame seed", "sesame paste",
        ],
    },
    "gluten": {
        "roots": [
            "FOODON_03420177",  # gluten
            "FOODON_00001907",  # gluten refined food product
            "FOODON_03310809",  # wheat gluten
            "FOODON_03310808",  # soy gluten
            "FOODON_03302452",  # gluten bread
            "FOODON_03302453",  # gluten flour
            "FOODON_03306200",  # gluten noodle
            "FOODON_00001275",  # wheat (big three)
            "FOODON_00001217",  # barley (big three)
            "FOODON_00001272",  # rye (big three)
            "FOODON_00001254",  # oats (cross-contamination risk)
        ],
        "keywords": [
            "gluten", "wheat", "barley", "rye", "spelt", "kamut", "farro",
            "durum", "bulgur", "malt", "soy sauce", "seitan",
            "brewer's yeast", "modified food starch", "roux", "gravy",
            "oats",
        ],
    },
    "celery": {
        "roots": [
            "FOODON_00001704",  # celery food product
            "FOODON_00001705",  # leaf celery food product
        ],
        "keywords": ["celery", "celeriac", "celery seed", "celery salt"],
    },
    "mustard": {
        "roots": ["FOODON_00002053"],  # mustard food product
        "keywords": [
            "mustard", "mustard seed", "mustard powder", "mustard flour",
        ],
    },
    "sulphites": {
        # Sulphites are additives rather than a FoodOn food-product branch,
        # so they are detected from explicit ingredient/additive names only.
        "roots": [],
        "keywords": [
            "sulphite", "sulphites", "sulfite", "sulfites",
            "sulphur dioxide", "sulfur dioxide", "metabisulphite",
            "metabisulfite", "bisulphite", "bisulfite", "sodium sulphite",
            "sodium sulfite", "potassium sulphite", "potassium sulfite",
            "e220", "e221", "e222", "e223", "e224", "e225", "e226", "e227",
            "e228",
        ],
    },
    "lupin": {
        "roots": [
            "FOODON_00001206",  # lupin seed food product
            "FOODON_00002012",  # lupine bean food product
        ],
        "keywords": ["lupin", "lupine", "lupin bean", "lupini", "lupin flour"],
    },
    "molluscs": {
        "roots": ["FOODON_00002044"],  # mollusc food product
        "keywords": [
            "mollusc", "mollusk", "clam", "mussel", "oyster", "scallop",
            "squid", "octopus", "cuttlefish", "whelk", "cockle", "abalone",
            "snail",
        ],
    },
}

MILK_PLANT_EXCLUSION_REGEXES: list[str] = [
    r".*\b(coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"([ -]+(flavoured|flavored))?[ -]+(milk|cream|yogurt|yoghurt)\b.*",
    r".*\b(milk|cream|yogurt|yoghurt)[ -]+alternative\b.*",
    r".*\bnon[ -]*dairy\b.*",
    r".*\bdairy[ -]*free\b.*",
    r".*\bplant[ -]*based\b.*",
    r".*\bvegan\b.*",
    r".*\b(peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter\b.*",
    r".*\bbutter[ -]*beans?\b.*",
    r".*\bbeans?,[ -]*butter\b.*",
    r".*\bbutternut\b.*",
    r".*\bcream[ -]+substitute\b.*",
    # Known lossy canonical forms produced from non-dairy source phrases.
    r"^(powdered butter|cream rice|cream parsley|milk rice|coconut paste milk"
    r"|butter almond|oil cocoa butter|butter paper)$",
    r"^cream sherry$",
]

GLUTEN_SAFE_REGEXES: list[str] = [
    r".*\bgluten[ -]*free\b.*",
    # HealthyFoods canonicalization removed "free" from these source terms.
    r"^gluten[ -]+(baking flour|self raising flour|flour|soy sauce|bread|"
    r"pasta|flour almond coconut|flour mix|bread mix)$",
    r".*\bbuckwheat\b.*",
    r".*\b(rice|tapioca|potato|almond|coconut|besan|chickpea|corn|maize|"
    r"quinoa|cassava|arrowroot)[ -]+flour\b.*",
    r".*\b(rice|pulse|chickpea|corn|maize|quinoa)[ -]+"
    r"(noodles?|pasta|spaghetti)\b.*",
    r".*\btamari\b.*",
    r"^(ground|minced|fresh|crystallized|crystallised|pickled|glace)?"
    r"[ -]*ginger$",
    r".*\b(wine|vinegar|vinaigrette)\b.*",
]

ALLERGEN_EXCLUSION_REGEXES: dict[str, list[str]] = {
    "milk": MILK_PLANT_EXCLUSION_REGEXES,
    "gluten": GLUTEN_SAFE_REGEXES,
    "wheat": GLUTEN_SAFE_REGEXES,
}


CONSUMER_GROUP_IRIS: dict[str, str] = {
    "coeliac": f"{FATO_NAMESPACE}Coeliac",
    "vegan": f"{FATO_NAMESPACE}Vegan",
    "vegetarian": f"{FATO_NAMESPACE}Vegetarian",
    "halal": f"{FATO_NAMESPACE}Halal",
    "kosher": f"{FATO_NAMESPACE}Kosher",
    "infant": f"{FATO_NAMESPACE}Infant",
    "elderly": f"{FATO_NAMESPACE}Elderly",
}

SUITABILITY_STATUSES = frozenset({"suitable", "not_suitable", "unknown"})


def aggregate_recipe_suitability(
    evidence: Iterable[Mapping[str, Any]],
    *,
    ingredient_count: int,
    groups: Iterable[str] = CONSUMER_GROUP_IRIS,
) -> dict[str, str]:
    """Derive recipe suitability without treating missing facts as safe.

    ``evidence`` contains known ingredient-to-consumer-group assessments.
    A single ``not_suitable`` ingredient rejects the recipe. A recipe is
    ``suitable`` only when every ingredient has explicit suitable evidence.
    All incomplete or conflicting cases remain ``unknown``.
    """

    rows = list(evidence)
    result: dict[str, str] = {}
    for group in groups:
        group_rows = [
            row
            for row in rows
            if str(row.get("group") or "").strip().casefold() == group
        ]
        statuses = {
            str(row.get("status") or "").strip().casefold()
            for row in group_rows
        } & SUITABILITY_STATUSES
        if "not_suitable" in statuses:
            result[group] = "not_suitable"
            continue

        covered_ingredients = {
            str(row.get("ingredient_key") or "").strip()
            for row in group_rows
            if row.get("ingredient_key")
            and str(row.get("status") or "").strip().casefold() == "suitable"
        }
        if ingredient_count > 0 and len(covered_ingredients) == ingredient_count:
            result[group] = "suitable"
        else:
            result[group] = "unknown"
    return result
