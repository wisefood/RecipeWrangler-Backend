"""Versioned vegan/vegetarian composition rules shared by graph projections."""

from __future__ import annotations

import re
from typing import Any

from recipe_wrangler.utils.food_ontology import (
    DAIRY_PRODUCT_KEYWORDS,
    MEAT_PRODUCT_KEYWORDS,
)

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
            *DAIRY_PRODUCT_KEYWORDS,
            *MEAT_PRODUCT_KEYWORDS,
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
            *MEAT_PRODUCT_KEYWORDS,
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
            *DAIRY_PRODUCT_KEYWORDS,
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


# Plain plant foods that carry no FoodOn class under a plant-origin root (sugar, tomatoes, spring onions, baking powder,
# vinegar, herbs...). Full-name patterns only (optionally with colour/form words), so "tomato" is a hit but
# "tomato and beef pie" is not. Positive evidence for BOTH vegan and vegetarian; a blocking keyword or origin still wins
# because classify_vegan_vegetarian never overrides "not_suitable". Sugar is treated as suitable (bone-char refining is not
# tracked in the ingredient name). Added 2026-09-26 after the corrected-parse sync left 8,180 ingredient nodes "unknown".
MODS = r"(?:fresh|dried|dry|ground|chopped|diced|sliced|minced|grated|crushed|whole|large|small|medium|baby|red|green|yellow|white|black|brown|orange|purple|golden|sweet|hot|mild|extra|virgin|light|dark|plain|self[- ]raising|self[- ]rising|wholemeal|wholewheat|whole[- ]wheat|all[- ]purpose|strong|bread|cake|caster|castor|icing|powdered|granulated|granulated|raw|organic|frozen|canned|tinned|ripe|new|cherry|plum|vine|roma|beef|cooking|fine|coarse|sea|table|kosher|flaked|flat[- ]leaf|curly|italian|thai|spanish|english|french|greek|mixed|instant|rolled|jumbo|porridge|pearl|long[- ]grain|short[- ]grain|basmati|jasmine|arborio|extra[- ]virgin|pure|natural|unsalted|salt[- ]reduced)"
NOUNS = [
 # seasonings and pantry
 r"salt", r"pepper(?:corns?)?", r"water", r"ice(?: cubes?)?", r"sugar", r"brown sugar", r"maple syrup", r"vinegar", r"(?:balsamic|red wine|white wine|cider|rice wine|malt|sherry) vinegar",
 r"baking (?:powder|soda)", r"bicarbonate of soda", r"bread soda", r"(?:dry |instant |active )?yeast", r"(?:corn ?flour|corn ?starch|cornstarch)", r"flour", r"(?:plain|self[- ]raising|wholemeal|strong|rye|spelt|bread) flour",
 r"cocoa(?: powder)?", r"vanilla (?:extract|essence)", r"mustard(?: seeds?| powder)?", r"dijon mustard", r"soy sauce", r"tomato (?:paste|puree|purée|sauce|passata)", r"passata", r"tahini", r"(?:olive|vegetable|sunflower|canola|rapeseed|sesame|coconut|groundnut|peanut|corn) oil",
 # herbs and spices
 r"basil", r"coriander(?: leaves| seeds)?", r"cilantro", r"parsley", r"mint(?: leaves)?", r"thyme", r"rosemary", r"oregano", r"sage", r"dill", r"chives?", r"bay leaves?", r"tarragon", r"marjoram",
 r"cumin(?: seeds)?", r"paprika", r"cinnamon", r"nutmeg", r"turmeric", r"cardamom", r"cloves?", r"allspice", r"cayenne(?: pepper)?", r"chilli(?: flakes| powder)?", r"chili(?: flakes| powder)?", r"curry powder", r"garam masala", r"mixed herbs", r"mixed spice", r"fennel seeds?", r"caraway seeds?", r"sesame seeds?", r"poppy seeds?", r"chia seeds?", r"sunflower seeds?", r"pumpkin seeds?", r"pepitas", r"linseeds?", r"flax ?seeds?",
 # vegetables and fruit
 r"tomatoes?", r"onions?", r"spring onions?", r"scallions?", r"shallots?", r"garlic(?: cloves?)?", r"ginger", r"leeks?", r"potato(?:es)?", r"sweet potato(?:es)?", r"kumara", r"carrots?", r"celery", r"courgettes?", r"zucchinis?", r"(?:bell )?peppers?", r"capsicums?", r"broccoli", r"broccolini", r"cauliflower", r"cabbage", r"spinach", r"kale", r"silverbeet", r"chard", r"lettuce", r"rocket", r"arugula", r"cucumbers?", r"radish(?:es)?", r"mushrooms?", r"peas", r"sugar snap peas", r"snow peas", r"green beans", r"sweetcorn", r"corn", r"pumpkin", r"butternut squash", r"squash", r"beetroot", r"beets?", r"eggplant", r"aubergine", r"avocados?", r"asparagus", r"fennel", r"celeriac", r"parsnips?", r"turnips?", r"bok choy", r"pak choi",
 r"apples?", r"bananas?", r"lemons?", r"limes?", r"oranges?", r"mandarins?", r"strawberr(?:y|ies)", r"blueberr(?:y|ies)", r"raspberr(?:y|ies)", r"blackberr(?:y|ies)", r"cranberr(?:y|ies)", r"mango(?:es|s)?", r"pineapple", r"peach(?:es)?", r"pears?", r"grapes?", r"dates?", r"raisins?", r"sultanas?", r"apricots?", r"plums?", r"cherr(?:y|ies)", r"kiwi ?fruits?", r"melon", r"watermelon", r"passion ?fruit", r"figs?", r"prunes?",
 r"(?:lemon|lime|orange) (?:juice|zest)", r"(?:lemon|lime|orange) (?:juice and zest|zest and juice)",
 # grains, legumes, nuts
 r"rice", r"oats", r"rolled oats", r"quinoa", r"couscous", r"barley", r"bulgur", r"lentils", r"red lentils", r"chickpeas", r"kidney beans", r"black beans", r"cannellini beans", r"borlotti beans", r"baked beans", r"beans", r"edamame(?: beans)?", r"tofu", r"tempeh",
 r"almonds?", r"walnuts?", r"cashews?", r"peanuts?", r"pecans?", r"hazelnuts?", r"pistachios?", r"pine nuts?", r"brazil nuts?", r"desiccated coconut", r"coconut", r"coconut milk", r"mixed nuts",
]
def pattern(noun): return rf"^(?:{MODS}[ ,-]+)*(?:{noun})(?:[ ,-]+(?:and|or|,)?[ ,-]*{MODS})*[ ,]*$"
PLANT_STAPLE_PATTERNS=[pattern(n) for n in NOUNS]


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
