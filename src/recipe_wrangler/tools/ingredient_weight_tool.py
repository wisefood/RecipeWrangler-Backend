# Purpose: Estimate ingredient weights (grams) using USDA portion/weight data.

import csv
import io
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Tuple

import re
from langchain.tools import tool

from recipe_wrangler.tools.ingredient_weight_llm_tool import ingredient_weight_llm_tool
from recipe_wrangler.tools.reviewed_item_weights import reviewed_default_unit, reviewed_item_grams
from recipe_wrangler.utils.non_food_ingredients import is_unambiguous_non_food_ingredient
from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.repositories.vector_matchers import VectorCollection
from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.usda_nutrients_v1 import canonical_name_to_usda, usda_id_to_link
from recipe_wrangler.utils.weigh_calculation_usda_ import (
    find_weight_match_by_name,
    match_portion,
    weight_from_density_fallback,
    weight_from_ingredient,
)


def _as_list(x: Any) -> list:
    if x is None:
        return []
    # Handle pandas/numpy NaN
    try:
        import math
        if isinstance(x, float) and math.isnan(x):
            return []
    except Exception:
        pass
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            import ast
            val = ast.literal_eval(x)
            if isinstance(val, list):
                return val
        except Exception:
            return [s.strip() for s in x.split(",") if s.strip()]
        return [x]
    try:
        return list(x)
    except Exception:
        return [x]


_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<qty>[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?(?:\s*-\s*[0-9]+(?:\.[0-9]+)?)?)\s*(?P<unit>.*)$"
)

_RANGE_QTY_RE = r"[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?"
_RANGE_MEASUREMENT_RE = re.compile(
    rf"^\s*(?P<q1>{_RANGE_QTY_RE})\s*(?:to|-|–|—)\s*(?P<q2>{_RANGE_QTY_RE})\s*(?P<rest>.*)$"
)
_SPLIT_MIXED_NUMBER_RE = re.compile(
    r"^\s*(?P<whole>[0-9]+)\s*-\s*(?P<fraction>[0-9]+/[0-9]+)\b(?P<rest>.*)$"
)
_SLASHLESS_MIXED_FRACTION_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<whole>\d+)\s+(?P<num>\d)(?P<den>[2348])\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
_SLASHLESS_SIMPLE_FRACTION_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<num>\d)(?P<den>[2348])\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
# Mass units restricted to den ∈ {2,4} because "12 oz" / "12 lb" can be a
# real can/package size, while 1/3 lb or 1/8 lb is rare in recipes. Only
# repair patterns where the implied integer quantity (12, 14, 34, 18) is
# implausible in a pound/ounce context.
_SLASHLESS_MIXED_FRACTION_MASS_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<whole>\d+)\s+(?P<num>\d)(?P<den>[24])\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
_SLASHLESS_SIMPLE_FRACTION_MASS_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<num>\d)(?P<den>[24])\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
# Slashless range fractions: "14-1 cup" → "1/4-1 cup", "14-13 cup" → "1/4-1/3 cup".
# Repaired output keeps the hyphen so the existing _RANGE_MEASUREMENT_RE
# can average the two endpoints.
_SLASHLESS_RANGE_FRACTION_VOLUME_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<n1>\d)(?P<d1>[2348])\s*-\s*"
    r"(?:(?P<n2>\d)(?P<d2>[2348])(?=\s)|(?P<int2>\d+))\s+"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?)\b",
    re.IGNORECASE,
)
_SLASHLESS_RANGE_FRACTION_MASS_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<n1>\d)(?P<d1>[24])\s*-\s*"
    r"(?:(?P<n2>\d)(?P<d2>[24])(?=\s)|(?P<int2>\d+))\s+"
    r"(?P<unit>lbs?|pounds?)\b",
    re.IGNORECASE,
)
# Detect a measurement that got fused into the start of an ingredient name,
# e.g. "12 lbs lean hamburger" sitting in the name field with measurement="1".
_NAME_LEADING_MEASUREMENT_RE = re.compile(
    r"^\s*\(?(?P<qty>\d+(?:\s+\d+/\d+|/\d+|\.\d+)?)\s*"
    r"(?P<unit>cups?|tbsp|tablespoons?|tsp|teaspoons?|"
    r"lbs?|pounds?|oz|ounces?|grams?|g|kg|ml|millilitres?|milliliters?|"
    r"l|litres?|liters?)\b\.?\)?\s+(?P<rest>\S.*)$",
    re.IGNORECASE,
)
# Recipe1M also fuses a bare unit abbreviation into the start of the name while
# leaving the quantity alone in the measurement field, e.g.
# name="c. grated Parmesan cheese", measurement="1/3" -> "1/3 cup grated ...".
_NAME_LEADING_UNIT_RE = re.compile(
    r"^\s*(?P<unit>c|cups?|tbsps?|tbs|tablespoons?|tsps?|teaspoons?|oz|ounces?|"
    r"lbs?|pounds?|pts?|pints?|qts?|quarts?|gals?|gallons?|pkgs?|packages?|"
    r"pkts?|packets?|mls?|millilit(?:re|er)s?|kgs?|kilograms?|g|grams?|"
    r"dash(?:es)?|pinch(?:es)?|cloves?|slices?|sticks?|sheets?|cans?|jars?|"
    r"bottles?|boxes?)\.?\s+(?P<rest>[A-Za-z(].*\S)\s*$",
    re.IGNORECASE,
)
_NAME_LEADING_UNIT_MAP = {
    "c": "cup", "cup": "cup", "cups": "cup",
    "tbsp": "tablespoon", "tbsps": "tablespoon", "tbs": "tablespoon",
    "tablespoon": "tablespoon", "tablespoons": "tablespoon",
    "tsp": "teaspoon", "tsps": "teaspoon", "teaspoon": "teaspoon", "teaspoons": "teaspoon",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "pt": "pint", "pts": "pint", "pint": "pint", "pints": "pint",
    "qt": "quart", "qts": "quart", "quart": "quart", "quarts": "quart",
    "gal": "gallon", "gals": "gallon", "gallon": "gallon", "gallons": "gallon",
    "pkg": "package", "pkgs": "package", "package": "package", "packages": "package",
    "pkt": "packet", "pkts": "packet", "packet": "packet", "packets": "packet",
    "ml": "ml", "mls": "ml", "millilitre": "ml", "millilitres": "ml",
    "milliliter": "ml", "milliliters": "ml",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "gram", "grams": "gram", "gram": "gram",
    "dash": "dash", "dashes": "dash", "pinch": "pinch", "pinches": "pinch",
    "dstspn": "dessertspoon", "dsp": "dessertspoon", "dessertspn": "dessertspoon",
    "clove": "clove", "cloves": "clove", "slice": "slice", "slices": "slice",
    "stick": "stick", "sticks": "stick", "sheet": "sheet", "sheets": "sheet",
    "can": "can", "cans": "can", "jar": "jar", "jars": "jar",
    "bottle": "bottle", "bottles": "bottle", "box": "box", "boxes": "box",
}
_MULTIPLIER_MASS_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<count>\d+(?:\.\d+)?)\s*x\s*(?P<size>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>g|grams?|kg|oz|ounces?|lb|lbs|pounds?)\b",
    re.IGNORECASE,
)
# A bare "<N> oz can" / "14.5 oz can" / "400 g tin" measurement: the leading
# number belongs to the package-size descriptor, not a count. Without this the
# "28" in "28 oz can" gets read as a 28x multiplier (→ 22 kg of tomatoes).
_PACKAGE_SIZE_PREFIX_MEASUREMENT_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*"
    r"(?:fl\.?\s*)?(?:oz|ounces?|lbs?|pounds?|g|grams?|kg|ml|millilitres?|"
    r"milliliters?|l|litres?|liters?)\.?\s+"
    r"(?P<container>cans?|jars?|tins?|bottles?|cartons?|pouches?|boxes?|bags?|"
    r"packages?|packets?|pkgs?|containers?|tubs?)\b",
    re.IGNORECASE,
)

_UNICODE_FRACTIONS = {
    "½": "1/2",
    "⅓": "1/3",
    "⅔": "2/3",
    "¼": "1/4",
    "¾": "3/4",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

_WORD_QUANTITIES = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "half": 0.5,
    "quarter": 0.25,
    "couple": 2.0,
    "few": 3.0,
    "handful": 1.0,
    "dash": 1.0,
    "splash": 1.0,
    "pinch": 1.0,
}

_COUNTABLE_NOUNS = {
    "clove": "clove",
    "cloves": "clove",
    "sprig": "sprig",
    "sprigs": "sprig",
    "leaf": "leaf",
    "leaves": "leaf",
    "stalk": "stalk",
    "stalks": "stalk",
    "stick": "stick",
    "sticks": "stick",
    "slice": "slice",
    "slices": "slice",
    "piece": "piece",
    "pieces": "piece",
    "whole": "whole",
    "wholes": "whole",
    "head": "head",
    "heads": "head",
    "fillet": "fillet",
    "fillets": "fillet",
    "filet": "fillet",
    "filets": "fillet",
    "chop": "chop",
    "chops": "chop",
    "bun": "bun",
    "buns": "bun",
    "bagel": "bagel",
    "bagels": "bagel",
    "bunch": "bunch",
    "bunches": "bunch",
    "bag": "bag",
    "bags": "bag",
    "box": "box",
    "boxes": "box",
    "can": "can",
    "cans": "can",
    "tin": "tin",
    "tins": "tin",
    "container": "container",
    "containers": "container",
    "jar": "jar",
    "jars": "jar",
    "package": "package",
    "packages": "package",
    "packet": "packet",
    "packets": "packet",
    "egg": "egg",
    "eggs": "egg",
    "white": "egg_white",
    "whites": "egg_white",
    "yolk": "egg_yolk",
    "yolks": "egg_yolk",
    "sheet": "sheet",
    "sheets": "sheet",
    "shell": "shell",
    "shells": "shell",
    "cube": "cube",
    "cubes": "cube",
    "bulb": "bulb",
    "bulbs": "bulb",
    "breast": "breast",
    "breasts": "breast",
    "prawn": "prawn",
    "prawns": "prawn",
    "muffin": "muffin",
    "muffins": "muffin",
    "envelope": "envelope",
    "envelopes": "envelope",
    "block": "block",
    "blocks": "block",
    "cake": "cake",
    "cakes": "cake",
    "punnet": "punnet",
    "punnets": "punnet",
    "avocado": "avocado",
    "avocados": "avocado",
    "tomato": "tomato",
    "tomatoes": "tomato",
    "peach": "peach",
    "peaches": "peach",
    "apple": "apple",
    "apples": "apple",
    "banana": "banana",
    "bananas": "banana",
    "onion": "onion",
    "onions": "onion",
    "shallot": "shallot",
    "shallots": "shallot",
    "carrot": "carrot",
    "carrots": "carrot",
    "potato": "potato",
    "potatoes": "potato",
    "jalapeno": "jalapeno",
    "jalapenos": "jalapeno",
    "chile": "chile",
    "chiles": "chile",
    "pepper": "pepper",
    "peppers": "pepper",
    "zucchini": "zucchini",
    "squash": "squash",
    "lemon": "lemon",
    "lemons": "lemon",
    "lime": "lime",
    "limes": "lime",
    "orange": "orange",
    "oranges": "orange",
    "celery": "celery",
    "garlic": "clove",
    "chilli": "chile",
    "chillies": "chile",
    "chili": "chile",
    "chilies": "chile",
    "capsicum": "pepper",
    "capsicums": "pepper",
    "leek": "whole",
    "leeks": "whole",
    "mango": "whole",
    "mangoes": "whole",
    "mangos": "whole",
    "eggplant": "whole",
    "eggplants": "whole",
    "aubergine": "whole",
    "aubergines": "whole",
    "beetroot": "whole",
    "beetroots": "whole",
    "parsnip": "whole",
    "parsnips": "whole",
    "mushroom": "whole",
    "mushrooms": "whole",
    "sausage": "sausage",
    "sausages": "sausage",
    "tortilla": "tortilla",
    "tortillas": "tortilla",
    "wrap": "wrap",
    "wraps": "wrap",
    "pita": "pita",
    "pitas": "pita",
    "pitta": "pita",
    "pittas": "pita",
    "roll": "roll",
    "rolls": "roll",
    "thigh": "thigh",
    "thighs": "thigh",
    "drumstick": "drumstick",
    "drumsticks": "drumstick",
    "shank": "shank",
    "shanks": "shank",
    "cutlet": "cutlet",
    "cutlets": "cutlet",
    "tenderloin": "tenderloin",
    "tenderloins": "tenderloin",
    "scallop": "scallop",
    "scallops": "scallop",
    "pear": "pear",
    "pears": "pear",
    "strawberry": "strawberry",
    "strawberries": "strawberry",
    "kiwi": "kiwi",
    "kiwis": "kiwi",
    "kiwifruit": "kiwi",
    "plum": "plum",
    "plums": "plum",
    "apricot": "apricot",
    "apricots": "apricot",
    "prune": "prune",
    "prunes": "prune",
    "fig": "fig",
    "figs": "fig",
    "grapefruit": "grapefruit",
    "grapefruits": "grapefruit",
    "mandarin": "mandarin",
    "mandarins": "mandarin",
    "pomegranate": "pomegranate",
    "pomegranates": "pomegranate",
    "date": "date",
    "dates": "date",
    "olive": "olive",
    "olives": "olive",
    "grape": "grape",
    "grapes": "grape",
    "raisin": "raisin",
    "raisins": "raisin",
    "pod": "pod",
    "pods": "pod",
    "anchovy": "fillet",
    "anchovies": "fillet",
    "rasher": "rasher",
    "rashers": "rasher",
    "cob": "cob",
    "cobs": "cob",
    "base": "base",
    "bases": "base",
    "finger": "finger",
    "fingers": "finger",
    "biscuit": "biscuit",
    "biscuits": "biscuit",
    "scallion": "scallion",
    "scallions": "scallion",
    "passionfruit": "passionfruit",
    "nectarine": "nectarine",
    "nectarines": "nectarine",
    "swede": "swede",
    "swedes": "swede",
    "kumara": "kumara",
    "gherkin": "gherkin",
    "gherkins": "gherkin",
    "sweetcorn": "cob",
    "pineapple": "whole",
    "pineapples": "whole",
    "cabbage": "whole",
    "cabbages": "whole",
    "papaya": "whole",
    "papayas": "whole",
    "pumpkin": "whole",
    "pumpkins": "whole",
    "melon": "whole",
    "melons": "whole",
    "naan": "naan",
    "roti": "roti",
    "cracker": "cracker",
    "crackers": "cracker",
    "oatcake": "oatcake",
    "oatcakes": "oatcake",
}

_MASS_UNITS = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mg": 0.001,
    "milligram": 0.001,
    "milligrams": 0.001,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}

_UNIT_ALIASES = {
    "c": "cup",
    "cups": "cup",
    "tsps": "tsp",
    "teaspoons": "teaspoon",
    "tbsps": "tbsp",
    "tablespoons": "tablespoon",
    "tbs": "tablespoon",
    "dessertspoons": "dessertspoon",
    "dsp": "dessertspoon",
    "handfuls": "handful",
    "handfull": "handful",
    "pinches": "pinch",
    "dashs": "dash",
    "dashes": "dash",
    "sprays": "spray",
    "bunches": "bunch",
    "small-sized": "small",
    "medium-sized": "medium",
    "large-sized": "large",
    "springs": "sprig",
    "wedges": "wedge",
    "spears": "spear",
    "trays": "tray",
    "sprinkle": "sprinkling",
    "sprinkles": "sprinkling",
    "wholegrain": "whole",
    "wholemeal": "whole",
    "grainy": "whole",
    "ripe": "whole",
    "baby": "small",
    "mini": "small",
    "breads": "whole",
    "darne": "fillet",
    "darnes": "fillet",
    "sachet": "packet",
    "sachets": "packet",
}

_UNIT_STOPWORDS = {
    "of",
    "fresh",
    "ripe",
    "big",
    "whole",
    "raw",
}

_SIZE_WORDS = {"small", "medium", "large"}
_SIZE_FACTORS = {"small": 0.65, "medium": 1.0, "large": 1.35}
_TURNIP_SIZE_GRAMS = {"small": 61.0, "medium": 122.0, "large": 183.0}
_WATER_LIKE_TOKENS = {"water", "stock", "broth"}
_DRY_VOLUME_EXCLUSION_TOKENS = {
    "powder", "granule", "granules", "cube", "cubes", "dry", "paste",
    "concentrate", "concentrated", "mix", "seasoning",
}
_LIQUID_DENSITY_BY_TOKEN = {
    "oil": 0.92,
    "milk": 1.03,
    "cream": 0.99,
    "honey": 1.42,
    "syrup": 1.37,
    "vinegar": 1.01,
    "sauce": 1.19,
    "juice": 1.04,
    "wine": 0.99,
    "cider": 1.0,
    "beer": 1.0,
    "verjus": 1.0,
    "dressing": 1.0,
    "skimmed": 1.03,
    "semi-skimmed": 1.03,
    "puree": 1.03,
    "lager": 1.0,
    "kirsch": 0.95,
    "tokaji": 1.0,
    "unicum": 1.0,
    "antica": 1.0,
    "liqueur": 1.0,
    "marinade": 1.1,
}
_ML_UNITS = {"ml", "milliliter", "milliliters", "millilitre", "millilitres"}
_L_UNITS = {"l", "liter", "liters", "litre", "litres"}
_VOLUME_UNIT_ML = {
    "ml": 1.0,
    "milliliter": 1.0,
    "milliliters": 1.0,
    "millilitre": 1.0,
    "millilitres": 1.0,
    "l": 1000.0,
    "liter": 1000.0,
    "liters": 1000.0,
    "litre": 1000.0,
    "litres": 1000.0,
    "dl": 100.0,
    "deciliter": 100.0,
    "deciliters": 100.0,
    "decilitre": 100.0,
    "decilitres": 100.0,
    "pint": 473.176473,
    "pints": 473.176473,
    "pt": 473.176473,
    "pts": 473.176473,
    "quart": 946.352946,
    "quarts": 946.352946,
    "qt": 946.352946,
    "qts": 946.352946,
    "teaspoon": 5.0,
    "teaspoons": 5.0,
    "tsp": 5.0,
    "tablespoon": 15.0,
    "tablespoons": 15.0,
    "tbsp": 15.0,
    "cup": 240.0,
    "cups": 240.0,
    "fl oz": 29.5735,
    "fluid ounce": 29.5735,
    "fluid ounces": 29.5735,
    "dash": 0.5,
    "splash": 15.0,
    "dessertspoon": 10.0,
    "dstspn": 10.0,
    "dsp": 10.0,
}
_ZERO_MEASUREMENT_RE = re.compile(
    r"^\s*(?:to taste|as needed|optional|garnish|to serve)\s*$",
    re.IGNORECASE,
)
_QUALIFIER_WORDS = {
    "extra",
    "virgin",
    "extra virgin",
    "fresh",
    "freshly",
    "ground",
    "low",
    "low fat",
    "low-fat",
    "reduced",
    "reduced fat",
    "reduced-fat",
    "unsalted",
    "salted",
    "dried",
    "raw",
    "cooked",
    "chopped",
    "sliced",
    "grated",
    "peeled",
    "crushed",
    # Manner adverbs that precede/follow a prep verb ("roughly chopped",
    # "finely diced") — without these the adverb survives qualifier-stripping
    # even though the verb itself was removed, leaving noise like "roughly"
    # stuck to the ingredient name.
    "roughly",
    "finely",
    "thinly",
    "coarsely",
    "lightly",
    "thoroughly",
    "loosely",
    "gently",
    "evenly",
    "diced",
    "minced",
    "shredded",
    "julienned",
    "cubed",
    "halved",
    "quartered",
    "trimmed",
    "deveined",
    "seeded",
    "pitted",
    "cored",
    "zested",
    "torn",
    "shaved",
}

HERB_SPICE_FOOD_GROUP = "Spices and Herbs"
HERB_MISSING_UNIT_FALLBACK_UNIT = "pinch"
HERB_MISSING_UNIT_FALLBACK_GRAMS = 0.3
_PREPARED_SALAD_TOKENS = {
    "dressing", "fruit", "potato", "pasta", "tuna", "chicken", "egg",
    "bean", "macaroni", "coleslaw",
}
_FRESH_HERB_TOKENS = {
    "basil", "cilantro", "coriander", "dill", "mint", "parsley",
    "oregano", "rosemary", "sage", "tarragon", "thyme",
}
# "to taste" / "as needed" / "optional" lines: a fixed, negligible-but-nonzero
# weight so seasonings register a token contribution instead of vanishing to 0.
TO_TASTE_MIN_GRAMS = 0.5
GREASING_OIL_GRAMS = 2.0
# Verification switch: "false" disables the reviewed cup/item tables and the generic spoon convention so a run can be
# diffed against the audited behaviour. Production leaves it on.
REVIEWED_TABLES_ENABLED = os.getenv("REVIEWED_TABLES_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
# A recipe line with no amount must not show "0 g" to the end user (2026-09-25 product decision).
# Blank-quantity oil is about a teaspoon. This is a policy placeholder, NOT calibrated: the source-truth
# test had n=7 recipes with unspecified oil and error was flat from 0 to 40 g. Deep/pan-fried recipes absorb
# far more, so they stay listed in the audit's `unquantified_oil_recipes`. Anything else gets the negligible
# amount. Rows are flagged `blank_quantity_policy` and are not counted as measured coverage.
BLANK_OIL_GRAMS = 5.0
BLANK_DEFAULT_GRAMS = TO_TASTE_MIN_GRAMS
LARGE_BARE_NUMBER_GRAMS_THRESHOLD = 50.0
VEGETABLES_FOOD_GROUP = "Vegetables and Vegetable Products"
LLM_UNIT_GRAMS_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "fallbacks"
    / "ingredient_unit_grams.csv"
)
FDA_UNIT_GRAMS_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "fallbacks"
    / "ingredient_unit_grams_fda.csv"
)
LLM_UNIT_GRAMS_MATCH_TYPE = "llm_ingredient_unit_grams_fallback"
LLM_UNIT_GRAMS_NOTE = "LLM fallback from ingredient_unit_grams.csv"
FDA_UNIT_GRAMS_MATCH_TYPE = "fda_reference_portion_fallback"
FDA_UNIT_GRAMS_NOTE = "FDA reference portion fallback from ingredient_unit_grams_fda.csv"
RECIPE1M_LLM_WEIGHT_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "recipe1m"
    / "recipe1m-unmatched-ingredient-weights-llm.csv"
)
RECIPE1M_LLM_WEIGHT_MATCH_TYPE = "llm_weight_fallback"
RECIPE1M_LLM_WEIGHT_NOTE = "LLM fallback from recipe1m-unmatched-ingredient-weights-llm.csv"
RECIPE1M_LLM_PORTION_FALLBACK_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "recipe1m"
    / "food_weights_updated.csv"
)
RECIPE1M_LLM_PORTION_FALLBACK_MATCH_TYPE = "llm_portion_per_unit_fallback"
RECIPE1M_LLM_PORTION_FALLBACK_NOTE = "LLM per-unit fallback from food_weights_updated.csv"
OFFLINE_REFERENCE_DATASET_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "weight_reference"
    / "ingredient_unit_reference_dataset.csv"
)
REVIEWED_OPENROUTER_REFERENCE_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "ingredient_parsing_final"
    / "2026-09-24"
    / "weight"
    / "reviewed_openrouter_weight_references.csv"
)
OFFLINE_REFERENCE_DATASET_ENABLED = os.getenv(
    "OFFLINE_REFERENCE_DATASET_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
OFFLINE_LLM_REBUILT_ENABLED = os.getenv(
    "OFFLINE_LLM_REBUILT_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
OFFLINE_REFERENCE_MIN_CONFIDENCE = float(
    os.getenv("OFFLINE_REFERENCE_MIN_CONFIDENCE", "0.7")
)
OFFLINE_REFERENCE_MATCH_TYPE = "offline_reference_dataset"
OFFLINE_REFERENCE_NOTE = (
    "Offline rebuild from ingredient_unit_reference_dataset.csv"
)
# Both Recipe1M LLM CSVs were generated by an 8B model with no verifier and
# contain a high proportion of fabricated values (default 0.8 confidence,
# 1L=1kg blanket, qty arithmetic errors). Gate them off by default; flip the
# env flag back on only after a verified rebuild lands.
RECIPE1M_LLM_FALLBACK_ENABLED = os.getenv(
    "RECIPE1M_LLM_FALLBACK_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
CACHED_LLM_UNIT_GRAMS_ENABLED = os.getenv(
    "CACHED_LLM_UNIT_GRAMS_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# Off by default (2026-09-25): a live LLM estimate would replace any low-confidence reviewed/convention weight
# with an unreviewed number. Set LIVE_WEIGHT_LLM_ENABLED=true to opt in.
LIVE_WEIGHT_LLM_ENABLED = os.getenv(
    "LIVE_WEIGHT_LLM_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# Verifier guards for live/cached LLM portion estimates. An 8B sometimes leaks a
# unit-conversion constant when it should be estimating a portion (e.g. "28.35 g
# per clove" = an ounce in grams). Map each tell-tale constant to the units for
# which it is actually a legitimate value; flag it for any other unit.
LLM_SUSPICIOUS_DEFAULT_WEIGHTS = {
    28.35: {"oz", "ounce", "ounces", "fl oz", "fluid ounce", "fluid ounces"},
    28.3495: {"oz", "ounce", "ounces", "fl oz", "fluid ounce", "fluid ounces"},
    453.59: {"lb", "lbs", "pound", "pounds"},
    453.592: {"lb", "lbs", "pound", "pounds"},
    1134.59: {"lb", "lbs", "pound", "pounds"},
    1000.0: {"l", "liter", "liters", "litre", "litres", "kg", "kilogram", "kilograms"},
}
LLM_IMPLAUSIBLE_PER_UNIT_WEIGHT_GRAMS = 5000.0
HERB_SPICE_HINT_TOKENS = {
    "herb",
    "spice",
    "seasoning",
    "powder",
    "pepper",
    "basil",
    "oregano",
    "thyme",
    "rosemary",
    "cumin",
    "paprika",
    "cinnamon",
    "nutmeg",
    "clove",
    "dill",
    "parsley",
    "sage",
    "turmeric",
    "chili",
    "chilli",
    "coriander",
    "tarragon",
    "mint",
    "bay leaf",
    "cardamom",
    "masala",
    "sumac",
    "dukkah",
    "shichimi",
    "zaatar",
}
PINCH_FALLBACK_EXCLUDED_TOKENS = {
    "scallion",
    "scallions",
    "green",
    "onion",
    "onions",
    "spring",
    "shallot",
    "shallots",
    "garlic",
    "chive",
    "chives",
    "leek",
    "leeks",
}
WHOLE_ANIMAL_TOKENS = {
    "duck",
    "chicken",
    "turkey",
    "goose",
    "hen",
    "pheasant",
    "quail",
    "partridge",
    "squab",
    "rabbit",
}
ALCOHOL_TOKENS = {"vodka", "wine", "liqueur", "rum", "gin", "whiskey", "bourbon", "brandy", "tequila", "beer", "absinthe"}
STOCK_SAUCE_TOKENS = {"stock", "broth", "bouillon", "sauce", "gravy", "dip", "dressing", "soup"}
GELATIN_THICKENER_TOKENS = {"gelatin", "jello", "jell", "pectin", "agar"}
BAKING_POWDER_TOKENS = {"baking powder", "baking soda", "cream of tartar"}
BAKING_MIX_TOKENS = {"bread mix", "cake mix", "brownie mix", "muffin mix", "pancake mix", "biscuit mix"}
RICE_GRAIN_TOKENS = {"rice", "oatmeal", "oats", "barley", "quinoa", "couscous", "bulgur", "farro"}
PASTA_NOODLE_TOKENS = {"pasta", "noodle", "noodles", "spaghetti", "macaroni", "penne", "linguine"}
BREAD_BAKED_TOKENS = {
    "bread",
    "roll",
    "rolls",
    "bun",
    "buns",
    "bagel",
    "bagels",
    "biscuit",
    "biscuits",
    "pretzel",
    "pretzels",
    "cracker",
    "crackers",
    "cake",
    "cookie",
    "cookies",
    "pie",
    "pizza",
    "shell",
    "tart",
    "wafer",
    "wafers",
}
EGG_TOKENS = {"egg", "eggs"}
CHEESE_TOKENS = {"cheese", "cheddar", "mozzarella", "parmesan", "swiss", "feta", "ricotta"}
MILK_CREAM_TOKENS = {"milk", "cream", "yogurt", "yoghurt", "sour cream", "buttermilk"}
BEEF_TOKENS = {"beef", "steak", "veal", "brisket", "sirloin"}
LAMB_GOAT_TOKENS = {"lamb", "mutton", "sheep", "goat"}
PORK_TOKENS = {"pork", "ham", "bacon", "sausage", "prosciutto"}
POULTRY_TOKENS = {"duck", "chicken", "turkey", "goose", "hen", "pheasant", "quail", "partridge", "squab"}
FISH_SHELLFISH_TOKENS = {"fish", "salmon", "tuna", "cod", "trout", "halibut", "anchovy", "crab", "clam", "clams", "shrimp", "prawn", "lobster", "scallop", "octopus"}
LEGUME_TOKENS = {"bean", "beans", "lentil", "lentils", "pea", "peas", "chickpea", "chickpeas", "soy", "tofu"}
FRUIT_TOKENS = {"apple", "banana", "orange", "lemon", "lime", "pear", "peach", "berry", "berries", "cherry", "cherries", "pineapple", "mango", "grape", "grapes", "watermelon", "baobab"}
VEGETABLE_TOKENS = {"scallion", "scallions", "onion", "onions", "garlic", "leek", "leeks", "carrot", "carrots", "potato", "potatoes", "tomato", "tomatoes", "pepper", "peppers", "jalapeno", "lettuce", "cucumber", "cucumbers", "corn", "celery", "spinach", "broccoli"}
HERB_SPICE_CLASS_TOKENS = {"basil", "oregano", "thyme", "rosemary", "cumin", "paprika", "cinnamon", "nutmeg", "turmeric", "coriander", "parsley", "sage", "mint", "peppercorn", "peppercorns", "spice", "spices", "seasoning"}
OIL_FAT_TOKENS = {"oil", "shortening", "lard", "margarine", "butter", "ghee"}
SWEETENER_TOKENS = {"sugar", "honey", "syrup", "molasses", "agave"}
LEAFY_GREEN_BUNCH_TOKENS = {"spinach", "kale", "chard", "collard", "collards", "greens"}
SOFT_HERB_BUNCH_TOKENS = {"parsley", "cilantro", "coriander"}
SPRIG_HERB_TOKENS = {
    "basil",
    "cilantro",
    "coriander",
    "dill",
    "mint",
    "oregano",
    "parsley",
    "rosemary",
    "sage",
    "tarragon",
    "thyme",
}
HARD_CHEESE_VOLUME_TOKENS = {
    "cheddar",
    "mozzarella",
    "parmesan",
    "swiss",
    "feta",
    "colby",
    "jack",
    "monterey",
    "gouda",
    "provolone",
}
SOFT_CHEESE_EXCLUDED_PHRASES = {"cream cheese", "cottage cheese", "ricotta cheese"}
HERB_LEAF_TOKENS = SPRIG_HERB_TOKENS | {"bay"}
GELATIN_PACKET_TOKENS = {"gelatin", "jello", "jell"}
PEPPER_PINCH_TOKENS = {"pepper", "peppercorn", "peppercorns"}
ICE_TOKENS = {"ice", "cube", "cubes"}
GENERIC_LLM_PACKAGE_UNITS = {
    "bag",
    "box",
    "can",
    "container",
    "jar",
    "package",
    "packet",
}
GENERIC_LLM_PACKAGE_WEIGHTS = {340.0, 425.0, 454.0}
PROTEIN_IDENTITY_TOKENS = (
    POULTRY_TOKENS
    | BEEF_TOKENS
    | PORK_TOKENS
    | FISH_SHELLFISH_TOKENS
    | {"rabbit"}
)


USDA_LINKS_EMBED_COLLECTIONS = tuple(
    c.strip()
    for c in os.getenv(
        "USDA_LINKS_EMBED_COLLECTIONS",
        "usda_ingredients",
    ).split(",")
    if c.strip()
)
USDA_LINKS_EMBED_QUERY_K = int(os.getenv("USDA_LINKS_EMBED_QUERY_K", "20"))
USDA_LINKS_LEXICAL_QUERY_K = int(os.getenv("USDA_LINKS_LEXICAL_QUERY_K", "20"))
USDA_LINKS_HYBRID_QUERY_K = int(os.getenv("USDA_LINKS_HYBRID_QUERY_K", "8"))
USDA_LINKS_EMBED_MAX_DISTANCE = float(
    os.getenv("USDA_LINKS_EMBED_MAX_DISTANCE", "0.45")
)
USDA_LINKS_EMBED_MIN_CONFIDENCE = float(
    os.getenv("USDA_LINKS_EMBED_MIN_CONFIDENCE", "0.65")
)
USDA_QUERY_ALIASES = (
    (re.compile(r"\byoghurts?\b"), "yogurt"),
    (re.compile(r"\bbreadcrumbs?\b"), "bread crumbs"),
    (re.compile(r"\baubergines?\b"), "eggplant"),
    (re.compile(r"\bcourgettes?\b"), "zucchini"),
    (re.compile(r"\bsilverbeet\b"), "swiss chard"),
    (re.compile(r"\bfris[eé]e\b"), "endive"),
    (re.compile(r"\brockmelon\b"), "cantaloupe"),
    (re.compile(r"\bprawns?\b"), "shrimp"),
    (re.compile(r"\bcornflour\b"), "cornstarch"),
    (re.compile(r"\bpepitas?\b"), "pumpkin seeds"),
    (re.compile(r"\bcraisins?\b"), "dried cranberries"),
    (re.compile(r"\blinseeds?\b"), "flaxseed"),
    (re.compile(r"\b(?:orzo|risoni)\b"), "pasta"),
    (re.compile(r"\bbroccolini\b"), "broccoli"),
    (re.compile(r"\b(?:bok\s+choy|pak\s+choi)\b"), "chinese cabbage"),
    (re.compile(r"\bgai\s+lan\b"), "chinese broccoli"),
    (re.compile(r"\bcapsicums?\b"), "bell pepper"),
    (re.compile(r"\bbocconcini\b"), "mozzarella"),
    (re.compile(r"\bkumara\b"), "sweet potato"),
    (re.compile(r"\bcavolo\s+nero\b"), "kale"),
    (re.compile(r"\bscallions?\b"), "green onion"),
    (re.compile(r"\bbeansprouts?\b"), "bean sprouts"),
    (re.compile(r"\bgelatine\b"), "gelatin"),
    (re.compile(r"\bgingerroot\b"), "ginger"),
    (re.compile(r"\bbranzino\b"), "sea bass"),
    (re.compile(r"\bpaw\s*paw\b"), "papaya"),
)
# Grams per US cup (240 ml) for volume measures of common foods. Values are from the local USDA SR
# portion table (data/processed/usda/usda-weights.json) except rows marked "convention". The
# generic USDA name lookup misses many of these (caster/icing sugar, rolled oats, tomato paste...),
# which left 100+ volume rows unresolved.
_REVIEWED_CUP_GRAMS = (
    (re.compile(r"\b(?:icing|powdered|confectioners?|frosting)\s+sugar\b|\bicing\b"), 120.0, "powdered sugar (USDA unsifted)"),
    (re.compile(r"\b(?:caster|castor|granulated|golden caster|white)\s+sugar\b|\bsugar\b(?!.*(?:brown|icing|powdered|snap))"), 200.0, "granulated sugar (USDA)"),
    (re.compile(r"\b(?:self[- ]raising|self[- ]rising|plain|all[- ]purpose|white|baking)\s+flour\b|\bflour\b(?!.*whole)"), 125.0, "white flour (USDA)"),
    (re.compile(r"\b(?:wholemeal|whole[- ]?wheat|whole grain)\b.*\bflour\b|\bflour\b.*\bwhole"), 120.0, "whole-grain flour (USDA)"),
    (re.compile(r"\b(?:biscuit|baking|bread|pancake)\s+mix\b"), 125.0, "flour-based mix (convention: white flour)"),
    (re.compile(r"\bwheat[- ]?germ\b"), 115.0, "wheat germ (USDA)"),
    (re.compile(r"\b(?:rolled|porridge|jumbo|old[- ]fashioned)?\s*oats?\b|\boatmeal\b"), 81.0, "rolled oats (USDA regular, dry)"),
    (re.compile(r"\btomato\s+(?:paste|pur[eé]e)\b|\bsun[- ]?dried tomato paste\b"), 262.0, "tomato paste (USDA canned)"),
    (re.compile(r"\bchia\b"), 192.0, "chia seeds (convention: 12 g/tbsp)"),
    (re.compile(r"\b(?:flax(?:seed)?|linseed|lsa)\b"), 112.0, "ground flaxseed (USDA 7 g/tbsp)"),
    (re.compile(r"\bblackcurrants?\b|\bcurrants?\b"), 112.0, "currants (USDA)"),
    (re.compile(r"\b(?:freekeh|farro|spelt|wheat berries)\b"), 184.0, "whole wheat grain (convention)"),
    (re.compile(r"\bsago\b|\btapioca\b"), 152.0, "tapioca pearls (USDA)"),
    (re.compile(r"\b(?:penne|rigatoni|fusilli|macaroni|pasta)\b"), 100.0, "dry short pasta (USDA penne 95, elbows 122)"),
    (re.compile(r"\bcorn ?flakes\b"), 28.0, "corn flakes cereal (convention)"),
    (re.compile(r"\bchana dhal\b|\bsplit chickpeas\b"), 200.0, "split chickpeas dry (convention)"),
    (re.compile(r"\bbroccoslaw\b|\bslaw\b"), 90.0, "broccoli slaw (convention)"),
    (re.compile(r"\bedamame\b"), 155.0, "edamame (USDA)"),
    (re.compile(r"\bsun[- ]?dried tomatoes\b"), 54.0, "sun-dried tomatoes (USDA)"),
    (re.compile(r"\bpassion ?fruit\b"), 236.0, "passion fruit pulp (USDA)"),
    (re.compile(r"\bhummus\b"), 246.0, "hummus (USDA)"),
    (re.compile(r"\bspring onions?\b|\bscallions?\b"), 100.0, "spring onion chopped (USDA)"),
    (re.compile(r"\bribbons\b"), 100.0, "vegetable ribbons (convention)"),
    (re.compile(r"\bcorn ?flour\b|\bcorn ?starch\b"), 128.0, "cornstarch (USDA)"),
    (re.compile(r"\bgravy\b"), 128.0, "gravy granules (convention)"),
    (re.compile(r"\bbread ?crumbs\b"), 45.0, "fresh breadcrumbs (matches the existing lookup)"),
    (re.compile(r"\bsplenda\b"), 24.0, "granulated sucralose (convention)"),
    (re.compile(r"\bnatvia\b|\berythritol\b"), 190.0, "erythritol sweetener (convention)"),
    (re.compile(r"\bconserve\b"), 320.0, "jam (USDA)"),
    (re.compile(r"\bbaking soda\b|\bbicarbonate\b"), 220.0, "baking soda (USDA 4.6 g/tsp)"),
    (re.compile(r"\b(?:lemon|lime|orange)\s+(?:zest|peel|rind)\b|\bzest\b"), 96.0, "citrus zest (USDA lemon peel 6 g/tbsp)"),
    (re.compile(r"\b(?:curry|tandoori|laksa|chipotle|lemongrass|wasabi|tamarind|korma|sambal|gochujang)\b.*\b(?:paste|oelek)\b|\bgochujang\b|\bsambal oelek\b|\bmiso\b|\bpaste\b"), 275.0, "paste (USDA miso 275 g/cup)"),
    (re.compile(r"\b(?:active )?(?:dried )?yeast\b(?!.*extract)"), 135.0, "dry yeast (convention)"),
    (re.compile(r"\bnutritional yeast\b"), 80.0, "nutritional yeast flakes (convention)"),
    (re.compile(r"\b(?:fennel|mustard|poppy|sesame|caraway|cumin|coriander|celery|nigella) seeds?\b|\bpomegranate seeds\b"), 140.0, "seeds (convention ~3 g/tsp)"),
    (re.compile(r"\bflaked almonds\b|\bsliced almonds\b"), 92.0, "sliced almonds (USDA)"),
    (re.compile(r"\bpistachios?\b"), 123.0, "pistachios (USDA)"),
    (re.compile(r"\bpecans?\b|\bwalnuts?\b"), 100.0, "chopped nuts (USDA pecan halves 99)"),
    (re.compile(r"\b(?:red |green |sweet |white |brown |spring )?onions?\b(?!.*(?:powder|salt|flakes|soup))"), 160.0, "onion chopped (USDA)"),
    (re.compile(r"\bonion flakes\b"), 55.0, "dried onion flakes (convention)"),
    (re.compile(r"\bdry mustard\b|\bmustard powder\b"), 87.0, "dry mustard (convention)"),
    (re.compile(r"\b(?:prepared|dijon|wholegrain|yellow|brown)?\s*mustard\b(?!.*(?:seed|greens))"), 249.0, "prepared mustard (USDA)"),
    (re.compile(r"\bhorseradish\b"), 240.0, "prepared horseradish (convention)"),
    (re.compile(r"\b(?:kosher|flaky)\s+salt\b"), 190.0, "kosher salt (convention)"),
    (re.compile(r"\b(?:sea|himalayan|celery|table|pink)\s+salt\b|\bsalt\b"), 292.0, "salt (USDA table salt)"),
    (re.compile(r"\b(?:tzatziki|raita)\b|\bcream\b"), 245.0, "yogurt-based (USDA yogurt)"),
    (re.compile(r"\b(?:tomato relish|chutney|marmalade|redcurrant jelly|jelly)\b|\bcurrant jelly\b"), 320.0, "jam-like (USDA jam)"),
    (re.compile(r"\bunsalted butter\b|\bbutter\b"), 227.0, "butter (USDA)"),
    (re.compile(r"\bdark chocolate\b|\bchocolate\b"), 170.0, "chopped chocolate (convention)"),
    (re.compile(r"\bmuesli\b|\bgranola\b"), 85.0, "muesli (convention)"),
    (re.compile(r"\bgoji\b|\bdried fruit\b|\braisins?\b|\bsultanas?\b"), 150.0, "dried fruit (USDA raisins 145)"),
    (re.compile(r"\bdry milk\b|\bmilk powder\b|\bnonfat dry milk\b"), 120.0, "dry milk (USDA)"),
    (re.compile(r"\b(?:fresh )?chives\b"), 48.0, "chopped chives (USDA 3 g/tbsp)"),
    (re.compile(r"\bmarjoram\b|\boregano\b|\bthyme\b|\brosemary\b|\bsage\b|\bdried herbs?\b"), 30.0, "dried herb (USDA)"),
    (re.compile(r"\bextract\b|\bessence\b"), 208.0, "extract (USDA vanilla)"),
    (re.compile(r"\bbouillon granules\b|\bstock granules\b"), 160.0, "bouillon granules (convention)"),
    (re.compile(r"\barmagnac\b|\bbrandy\b|\brum\b|\bvodka\b|\bwhisk(?:e)?y\b|\bgin\b|\bsherry\b"), 228.0, "spirit (density ~0.95)"),
    (re.compile(r"\bhoney\b"), 339.0, "honey (USDA)"),
    (re.compile(r"\bmaple syrup\b"), 315.0, "maple syrup (USDA)"),
    (re.compile(r"\bgolden syrup\b|\bcorn syrup\b|\bsyrup\b"), 340.0, "syrup (convention)"),
    (re.compile(r"\bcooked chicken\b|\bchicken breast\b|\bbarbecue chicken\b"), 140.0, "cooked diced chicken (USDA)"),
    (re.compile(r"\bground (?:turkey|beef|pork)\b|\bmince\b"), 225.0, "raw ground meat (convention)"),
    (re.compile(r"\bcapers?\b"), 138.0, "capers (USDA 8.6 g/tbsp drained)"),
    (re.compile(r"\bolives?\b"), 134.0, "olives (USDA 8.4 g/tbsp)"),
    (re.compile(r"\b(?:baby\s+)?spinach\b"), 30.0, "spinach (USDA raw)"),
    (re.compile(r"\b(?:rocket|arugula)\b"), 20.0, "arugula (USDA raw)"),
    (re.compile(r"\b(?:salad|mesclun|lettuce|mixed)\s+(?:greens|leaves)\b|\bmixed salad\b|\bsalad (?:mix|bag)\b|\bmesclun\b"), 28.0, "salad leaves (USDA lettuce)"),
    (re.compile(r"(?<!split )(?<!black-eyed )(?<!black eyed )\b(?:frozen\s+)?peas\b"), 134.0, "frozen peas (USDA)"),
    (re.compile(r"(?<!cream )(?<!cottage )(?<!ricotta )(?<!mascarpone )\bcheese\b(?!\s*sauce)|\bcheddar\b|\bmozzarella\b"), 112.0, "shredded cheese (USDA mozzarella)"),
    (re.compile(r"\bpumpkin seeds?\b"), 129.0, "pumpkin seeds (USDA)"),
    (re.compile(r"\bsunflower seeds?\b"), 140.0, "sunflower seeds (USDA)"),
    (re.compile(r"\bcashews?\b"), 137.0, "cashews (USDA dry roasted)"),
    (re.compile(r"\b(?:peanut|nut|seed)\s+butter\b|\bsunflower seed butter\b"), 258.0, "nut butter (USDA peanut butter)"),
    (re.compile(r"\bred cabbage\b|\bcabbage\b"), 89.0, "cabbage chopped (USDA)"),
    (re.compile(r"\bcapsicum\b|\bbell pepper\b"), 149.0, "sweet pepper chopped (USDA)"),
    (re.compile(r"\bpineapple\b"), 165.0, "pineapple chunks (USDA)"),
    (re.compile(r"\b(?:banana|mango)\b"), 150.0, "sliced banana (USDA)"),
    (re.compile(r"\bapple\s+pur[eé]e\b|\bapplesauce\b"), 244.0, "applesauce (USDA)"),
    (re.compile(r"\b(?:black\s+)?treacle\b|\bmolasses\b"), 337.0, "molasses (USDA)"),
    (re.compile(r"\bjam\b|\bpreserves?\b"), 320.0, "jam (USDA 20 g/tbsp)"),
    (re.compile(r"\bvanilla\s+(?:essence|extract|paste|bean paste)\b"), 208.0, "vanilla extract (USDA)"),
    (re.compile(r"\b(?:dairy[- ]free\s+)?spread\b|\bmargarine\b"), 232.0, "margarine spread (USDA tub)"),
    (re.compile(r"\b(?:ground\s+)?(?:cookies|gingersnaps?|biscuits)\b"), 108.0, "cookie crumbs (convention: bread crumbs)"),
    (re.compile(r"\bgreen vegetables\b|\bvegetables\b"), 100.0, "chopped vegetables (convention)"),
    (re.compile(r"cr[eè]me fra[iî]che"), 240.0, "creme fraiche (USDA sour cream 230)"),
    (re.compile(r"\b(?:ground )?(?:caraway|cumin|paprika|cinnamon|nutmeg|turmeric|allspice|ginger)\b"), 120.0, "ground spice (USDA cinnamon 2.6 g/tsp)"),
)
# Unknown solid measured with a spoon: small amounts, so a rough convention beats "not converted".
_GENERIC_SPOON_GRAMS = {"teaspoon": 4.0, "tsp": 4.0, "dessertspoon": 8.0, "tablespoon": 12.0, "tbsp": 12.0}
LIVE_LLM_CONFIDENCE_THRESHOLD = float(
    os.getenv("LIVE_LLM_CONFIDENCE_THRESHOLD", "0.45")
)


@lru_cache(maxsize=1)
def _get_usda_links_collections() -> list:
    return [VectorCollection(name) for name in USDA_LINKS_EMBED_COLLECTIONS]


def _distance_to_similarity(distance: Optional[float]) -> Optional[float]:
    if distance is None:
        return None
    try:
        return 1.0 - float(distance)
    except (TypeError, ValueError):
        return None


def _search_tokens(text: Optional[str]) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return [_singularize_token(token) for token in tokens if len(token) > 1]


def _candidate_label(candidate: dict) -> str:
    return str(
        candidate.get("usda_food_label")
        or candidate.get("canonical")
        or candidate.get("name")
        or candidate.get("document")
        or ""
    )


def _usda_query_alias(name: str) -> str:
    """Translate reviewed regional names before USDA retrieval."""
    query = str(name or "").strip().lower()
    for pattern, replacement in USDA_QUERY_ALIASES:
        query = pattern.sub(replacement, query)
    return re.sub(r"\s+", " ", query).strip()


def _candidate_has_lexical_identity(query: str, candidate: dict) -> bool:
    """Reject embedding-only food identities that merely share a portion unit."""
    return any(
        _lexical_overlap_score(query, text) > 0.0
        for text in (_candidate_label(candidate), str(candidate.get("document") or ""))
    )


def _lexical_overlap_score(query: str, candidate_text: str) -> float:
    query_tokens = set(_search_tokens(query))
    candidate_tokens = set(_search_tokens(candidate_text))
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = query_tokens & candidate_tokens
    if not overlap:
        return 0.0
    precision = len(overlap) / len(candidate_tokens)
    recall = len(overlap) / len(query_tokens)
    return (2.0 * precision * recall / (precision + recall)) if precision + recall else 0.0


def _head_token(text: Optional[str]) -> Optional[str]:
    tokens = _search_tokens(text)
    if not tokens:
        return None
    stop = {
        "fresh", "dried", "dry", "fat", "free", "low", "reduced", "raw",
        "cooked", "prepared", "chopped", "sliced", "ground", "large",
        "medium", "small", "minute", "white", "wheat", "whole", "regular",
        "quick", "sweet",
    }
    for token in reversed(tokens):
        if token not in stop:
            return token
    return tokens[-1]


def _head_token_mismatch_reason(source_text: str, candidate_text: Optional[str]) -> Optional[str]:
    source_head = _head_token(source_text)
    candidate_head = _head_token(candidate_text)
    if not source_head or not candidate_head or source_head == candidate_head:
        return None
    protected_heads = {
        "rice", "bagel", "bun", "gelatin", "soup", "broth", "stock",
        "tongue", "scallion", "onion", "garlic", "sausage",
    }
    if source_head in protected_heads or candidate_head in protected_heads:
        return f"head_token_{source_head}_vs_{candidate_head}"
    return None


def _bm25_scores(query_tokens: list[str], corpus_tokens: list[list[str]]) -> list[float]:
    if not query_tokens or not corpus_tokens:
        return [0.0 for _ in corpus_tokens]

    doc_freq: dict[str, int] = {}
    for tokens in corpus_tokens:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    doc_count = len(corpus_tokens)
    avg_len = sum(len(tokens) for tokens in corpus_tokens) / max(1, doc_count)
    k1 = 1.5
    b = 0.75
    query_terms = set(query_tokens)
    scores: list[float] = []
    for tokens in corpus_tokens:
        if not tokens:
            scores.append(0.0)
            continue
        term_counts: dict[str, int] = {}
        for token in tokens:
            term_counts[token] = term_counts.get(token, 0) + 1
        doc_len = len(tokens)
        score = 0.0
        for term in query_terms:
            tf = term_counts.get(term, 0)
            if tf <= 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1.0 + (doc_count - df + 0.5) / (df + 0.5))
            denom = tf + k1 * (1.0 - b + b * doc_len / max(avg_len, 1e-9))
            score += idf * (tf * (k1 + 1.0) / denom)
        scores.append(score)

    max_score = max(scores) if scores else 0.0
    if max_score <= 0:
        return scores
    return [score / max_score for score in scores]


def _candidate_from_vector_hit(
    collection: Any,
    doc: Any,
    meta: Optional[dict],
    distance: Optional[float] = None,
    lexical_score: Optional[float] = None,
    source: str = "embedding",
) -> Optional[dict]:
    meta = meta or {}
    usda_id = str(meta.get("usda_id") or "").strip()
    if not usda_id:
        return None
    canonical = meta.get("usda_food_label") or meta.get("name") or doc
    similarity = _distance_to_similarity(distance)
    candidate = {
        "usda_id": usda_id,
        "canonical_id": meta.get("canonical_id"),
        "canonical": canonical,
        "usda_food_label": meta.get("usda_food_label"),
        "food_group_id": meta.get("food_group_id"),
        "food_group": meta.get("food_group"),
        "distance": distance,
        "similarity": similarity,
        "lexical_score": lexical_score,
        "match_source": source,
        "match_collection": getattr(collection, "name", None),
        "document": doc,
    }
    return candidate


@lru_cache(maxsize=1)
def _usda_links_lexical_index() -> list[dict]:
    rows: list[dict] = []
    for collection in _get_usda_links_collections():
        offset = 0
        page_size = 5000
        while True:
            try:
                page = collection.get(
                    limit=page_size,
                    offset=offset,
                    include=["documents", "metadatas"],
                )
            except Exception:
                break
            docs = page.get("documents") or []
            metas = page.get("metadatas") or []
            if not docs:
                break
            for doc, meta in zip(docs, metas):
                candidate = _candidate_from_vector_hit(
                    collection=collection,
                    doc=doc,
                    meta=meta,
                    source="lexical",
                )
                if not candidate:
                    continue
                label = _candidate_label(candidate)
                rows.append({
                    "candidate": candidate,
                    "tokens": _search_tokens(label),
                })
            if len(docs) < page_size:
                break
            offset += page_size
    return rows


def _lexical_usda_link_candidates(query: str) -> list[dict]:
    index = _usda_links_lexical_index()
    if not index:
        return []
    query_tokens = _search_tokens(query)
    scores = _bm25_scores(query_tokens, [row["tokens"] for row in index])
    ranked: list[dict] = []
    for row, score in zip(index, scores):
        if score <= 0:
            continue
        candidate = dict(row["candidate"])
        candidate["lexical_score"] = float(score)
        candidate["match_source"] = "hybrid_lexical"
        ranked.append(candidate)
    ranked.sort(key=lambda c: float(c.get("lexical_score") or 0.0), reverse=True)
    return ranked[: max(1, USDA_LINKS_LEXICAL_QUERY_K)]


def _hybrid_usda_candidate_score(candidate: dict, query: str) -> float:
    similarity = candidate.get("similarity")
    vector_score = 0.0
    if similarity is not None:
        try:
            vector_score = max(0.0, min(1.0, float(similarity)))
        except (TypeError, ValueError):
            vector_score = 0.0
    lexical_score = candidate.get("lexical_score")
    if lexical_score is None:
        lexical_score = _lexical_overlap_score(query, _candidate_label(candidate))
    try:
        lexical_score_f = max(0.0, min(1.0, float(lexical_score)))
    except (TypeError, ValueError):
        lexical_score_f = 0.0
    mismatch_penalty = 0.0
    if _head_token_mismatch_reason(query, _candidate_label(candidate)) is not None:
        mismatch_penalty = 0.2
    return max(0.0, 0.68 * vector_score + 0.32 * lexical_score_f - mismatch_penalty)


@lru_cache(maxsize=2048)
def _embedding_usda_link(name: str, unit: Optional[str] = None) -> Optional[dict]:
    original_query = str(name or "").strip()
    query = _usda_query_alias(original_query)
    if not query:
        return None

    collections = _get_usda_links_collections()
    if not collections:
        return None

    try:
        query_vec = get_embeddings(query)
    except Exception:
        return None

    all_candidates: list[dict] = []
    for collection in collections:
        try:
            results = collection.query(
                query_embeddings=[query_vec],
                n_results=max(1, USDA_LINKS_EMBED_QUERY_K),
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            continue

        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        for doc, meta, dist in zip(docs, metas, dists):
            distance = None
            if dist is not None:
                try:
                    distance = float(dist)
                except (TypeError, ValueError):
                    distance = None

            if (
                distance is not None
                and USDA_LINKS_EMBED_MAX_DISTANCE >= 0.0
                and distance > USDA_LINKS_EMBED_MAX_DISTANCE
            ):
                continue

            candidate = _candidate_from_vector_hit(
                collection=collection,
                doc=doc,
                meta=meta,
                distance=distance,
                source="embedding",
            )
            if not candidate:
                continue

            similarity = candidate.get("similarity")

            if (
                similarity is not None
                and USDA_LINKS_EMBED_MIN_CONFIDENCE >= 0.0
                and similarity < USDA_LINKS_EMBED_MIN_CONFIDENCE
            ):
                continue

            mismatch_reason = _usda_link_mismatch_reason(query, candidate)
            if mismatch_reason is not None:
                continue
            if not _candidate_has_lexical_identity(query, candidate):
                continue

            # Prefer candidates that can support the parsed non-mass unit,
            # then rank by minimum distance.
            supports_unit = True
            if unit and unit.strip().lower() not in _MASS_UNITS:
                usda_id = str(candidate.get("usda_id") or "").strip()
                if not match_portion(usda_id, unit, name=query):
                    supports_unit = False
            candidate["_supports_unit"] = supports_unit
            all_candidates.append(candidate)

    for candidate in _lexical_usda_link_candidates(query):
        if _usda_link_mismatch_reason(query, candidate) is not None:
            continue
        if not _candidate_has_lexical_identity(query, candidate):
            continue
        supports_unit = True
        usda_id = str(candidate.get("usda_id") or "").strip()
        if unit and unit.strip().lower() not in _MASS_UNITS:
            if not match_portion(usda_id, unit, name=query):
                supports_unit = False
        candidate["_supports_unit"] = supports_unit
        all_candidates.append(candidate)

    if not all_candidates:
        stripped_query = _strip_qualifiers(query)
        if stripped_query:
            stripped_hit = _embedding_usda_link(stripped_query, unit=unit)
            if stripped_hit:
                stripped_hit = dict(stripped_hit)
                stripped_hit["match_source"] = "embedding_qualifier_stripped"
                stripped_hit["original_query"] = query
                stripped_hit["stripped_query"] = stripped_query
                return stripped_hit
        return None

    deduped: dict[tuple[str, str], dict] = {}
    for candidate in all_candidates:
        key = (
            str(candidate.get("usda_id") or ""),
            str(candidate.get("canonical_id") or candidate.get("canonical") or ""),
        )
        candidate["hybrid_score"] = _hybrid_usda_candidate_score(candidate, query)
        existing = deduped.get(key)
        if existing is None or float(candidate.get("hybrid_score") or 0.0) > float(existing.get("hybrid_score") or 0.0):
            deduped[key] = candidate
    all_candidates = list(deduped.values())
    if not all_candidates:
        return None

    prefer_unit = bool(unit and unit.strip().lower() not in _MASS_UNITS)
    all_candidates.sort(
        key=lambda c: (
            0 if (c.get("_supports_unit") or not prefer_unit) else 1,
            -float(c.get("hybrid_score") or 0.0),
            float(c.get("distance")) if c.get("distance") is not None else float("inf"),
        )
    )
    all_candidates = all_candidates[: max(1, USDA_LINKS_HYBRID_QUERY_K)]

    best = dict(all_candidates[0])
    best.pop("_supports_unit", None)
    if query != original_query.lower():
        best["original_query"] = original_query
        best["aliased_query"] = query
    if str(best.get("match_source") or "") in {"embedding", "hybrid_lexical", "lexical"}:
        best["match_source"] = "hybrid"
    return best


def _clean_unit(unit_part: str) -> Optional[str]:
    unit_part = unit_part.strip()
    if not unit_part:
        return None
    tokens = [t.strip(".,") for t in unit_part.split()]
    tokens = [t for t in tokens if t]
    tokens = [t for t in tokens if not re.fullmatch(r"\d+(?:\.\d+)?", t)]
    if not tokens:
        return None

    # If any token clearly maps to a known countable noun, prefer it.
    for token in reversed(tokens):
        normalized = _UNIT_ALIASES.get(token, token)
        if normalized in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[normalized]
        if normalized in _VOLUME_UNIT_ML or normalized in {
            "pinch", "handful", "spray", "bunch", "sprig", "dash",
            "squeeze", "zest", "portion", "punnet", "cm", "inch",
        }:
            return normalized

    if len(tokens) >= 2:
        first_two = " ".join(tokens[:2])
        first_three = " ".join(tokens[:3])
        if first_three == "us fluid ounces":
            return "fluid ounces"
        if first_two in {"fl oz", "fluid ounce", "fluid ounces"}:
            return first_two
    filtered_tokens = [t for t in tokens if t not in _UNIT_STOPWORDS]
    if filtered_tokens:
        first_filtered = _UNIT_ALIASES.get(filtered_tokens[0], filtered_tokens[0])
        if first_filtered in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[first_filtered]
        return first_filtered

    first = _UNIT_ALIASES.get(tokens[0], tokens[0])
    if first in _COUNTABLE_NOUNS:
        return _COUNTABLE_NOUNS[first]
    return first


_GARBAGE_NAME_RE = re.compile(r"^[\W_]*$")
# Bare connector words seen standalone as a scraped "ingredient" (split
# artifacts of "X to Y" ranges, "2 x" multipliers) — never a real ingredient
# on their own. Exact-match only, not a length cutoff, so short real foods
# ("egg", "ice", "oil") are never touched.
_GARBAGE_NAME_WORDS = {"x", "to", "and", "or"}


def _is_garbage_ingredient_name(name: Any) -> bool:
    """True for scrape artifacts, never a real (even oddly-named) ingredient.

    Bare punctuation/underscore lines ("_", "...", ":", ".") and markdown-bold
    section headings captured as an ingredient row ("**Other Necessary
    Tools/Equipment**") are the two shapes seen in practice, plus a small
    exact-match set of connector words split out on their own.
    """
    text = str(name or "").strip()
    if not text:
        return False
    if _GARBAGE_NAME_RE.match(text):
        return True
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        return True
    if text.casefold() in _GARBAGE_NAME_WORDS:
        return True
    return False


def _normalize_for_duplicate_check(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\([^)]*\)", "", text).strip()  # drop "(as needed)" etc.
    text = re.sub(r"s$", "", text)  # crude singular/plural fold
    return text


_BLANK_NEGLIGIBLE_WORDS = {"water", "ice", "leaf", "leaves", "sprig", "sprigs"} | _FRESH_HERB_TOKENS


def _is_blank_negligible_ingredient(name: Any, measurement: Any) -> bool:
    # Water and garnish herbs with no quantity: nutrient contribution is nil at
    # any plausible amount, so the negligible placeholder is safe. Salt and oil
    # are deliberately excluded (sodium / kcal matter) and stay unresolved.
    if str(measurement or "").strip():
        return False
    words = [w for w in re.findall(r"[a-z]+", str(name or "").lower()) if w not in _QUALIFIER_WORDS]
    return bool(words) and all(w in _BLANK_NEGLIGIBLE_WORDS for w in words)


_SALT_NAME_WORDS = {
    "salt", "sea", "kosher", "table", "himalayan", "pink", "flaky", "rock", "fine", "coarse",
    "maldon", "celtic", "iodised", "iodized", "black", "smoked",
}


def _is_blank_salt(name: Any, measurement: Any) -> bool:
    # Product decision (2026-09-25): plain salt with no amount is one pinch. Sodium drives
    # Nutri-Score, so this is deliberately a policy value, flagged on the detail.
    text = str(measurement or "").strip()
    if text and not re.fullmatch(r"\d+(?:\.\d+)?", text):  # blank, or a bare number with no unit
        return False
    words = re.findall(r"[a-z]+", str(name or "").lower())
    return "salt" in words and all(w in _SALT_NAME_WORDS for w in words) and "black" not in words


def _is_zero_measurement(measurement: Any) -> bool:
    if measurement is None:
        return False
    text = str(measurement).strip()
    if _ZERO_MEASUREMENT_RE.match(text):
        return True
    # Some scrapers encode "to taste"/optional as a literal "0" quantity
    # ("0.0", "0 -", "0 tsp") rather than text — that parses as a real
    # numeric zero and multiplies every downstream weight to 0 g, not a
    # missing-quantity fallback. Treat a quantity that parses to exactly
    # 0 the same as the text phrases above.
    qty, _unit, _inferred = _split_measurement(measurement)
    if qty is not None:
        value = _parse_quantity_value(qty)
        if value is not None and value == 0.0:
            return True
    return False


def _strip_qualifiers(name: str) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9\s-]", " ", str(name or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return None

    tokens = normalized.split()
    stripped: list[str] = []
    i = 0
    while i < len(tokens):
        two = " ".join(tokens[i : i + 2])
        if two in _QUALIFIER_WORDS:
            i += 2
            continue
        if tokens[i] in _QUALIFIER_WORDS:
            i += 1
            continue
        stripped.append(tokens[i])
        i += 1

    stripped_name = " ".join(stripped).strip()
    if stripped_name and stripped_name != normalized:
        return stripped_name
    return None


def _liquid_density_for_name(
    name: str,
    food_group: Optional[str] = None,
    canonical: Optional[str] = None,
) -> Optional[tuple[float, str]]:
    name_norm = str(name or "").strip().lower()
    canonical_norm = str(canonical or "").strip().lower()
    food_group_norm = str(food_group or "").strip().lower()
    combined = " ".join(v for v in (name_norm, canonical_norm) if v)
    name_tokens = set(re.findall(r"[a-z]+", combined))
    if name_tokens & _WATER_LIKE_TOKENS:
        if name_tokens & _DRY_VOLUME_EXCLUSION_TOKENS:
            return None
        return 1.0, "water-like"
    if "maple" in name_tokens and "syrup" in name_tokens:
        return 1.37, "maple syrup"
    if "soy" in name_tokens and "sauce" in name_tokens:
        return 1.19, "soy sauce"
    if food_group_norm == "nut and seed products" and name_tokens & {
        "butter",
        "paste",
        "spread",
    }:
        return 1.10, "nut/seed butter"
    if "tahini" in name_tokens:
        return 1.10, "tahini"
    if "pesto" in name_tokens:
        return 1.00, "pesto"
    if name_tokens & {"ketchup", "harissa", "mayonnaise", "vegannaise"}:
        return 1.10, "thick condiment"
    if "kecap" in name_tokens and "manis" in name_tokens:
        return 1.20, "kecap manis"
    if "creme" in name_tokens and "fraiche" in name_tokens:
        return 1.00, "creme fraiche"
    if "quark" in name_tokens:
        return 1.00, "quark"
    if name_tokens & ALCOHOL_TOKENS:
        return 0.95, "alcoholic beverage"
    if name_tokens & {"yogurt", "yoghurt"}:
        return 1.03, "yogurt"
    if name_tokens & {"passata", "sherry", "beer", "stout", "mirin", "vinaigrette"}:
        return 1.00, "pourable food"
    if name_tokens & {"cider", "lager", "kirsch", "tokaji", "unicum", "antica", "liqueur"}:
        return 1.0, "alcoholic beverage"  # "dry cider" is a drink, not a dry ingredient
    if name_tokens & {"paste", "concentrate", "concentrated", "powder", "dry"}:
        return None
    for token, density in _LIQUID_DENSITY_BY_TOKEN.items():
        if token in name_tokens:
            return density, token
    return None


_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")


def _normalize_fraction_text(text: str) -> str:
    for symbol, replacement in _UNICODE_FRACTIONS.items():
        text = text.replace(symbol, replacement)
    # European decimal comma ("0,5 g") — recipe quantities are always small,
    # so a digit-comma-digit is a decimal point, never a thousands separator.
    text = _DECIMAL_COMMA_RE.sub(".", text)
    return text


def _repair_slashless_fraction_measurement(text: str) -> str:
    # Try ranges first so e.g. "14-13 cup" gets repaired to "1/4-1/3 cup"
    # before the simple-fraction matcher could grab just "14".
    range_vol = _SLASHLESS_RANGE_FRACTION_VOLUME_MEASUREMENT_RE.match(text)
    if range_vol:
        rhs = (
            range_vol.group("int2")
            if range_vol.group("int2")
            else f"{range_vol.group('n2')}/{range_vol.group('d2')}"
        )
        repaired = (
            f"{range_vol.group('n1')}/{range_vol.group('d1')}-{rhs} "
            f"{range_vol.group('unit')}"
        )
        return repaired + text[range_vol.end():]

    range_mass = _SLASHLESS_RANGE_FRACTION_MASS_MEASUREMENT_RE.match(text)
    if range_mass:
        rhs = (
            range_mass.group("int2")
            if range_mass.group("int2")
            else f"{range_mass.group('n2')}/{range_mass.group('d2')}"
        )
        repaired = (
            f"{range_mass.group('n1')}/{range_mass.group('d1')}-{rhs} "
            f"{range_mass.group('unit')}"
        )
        return repaired + text[range_mass.end():]

    mixed = _SLASHLESS_MIXED_FRACTION_MEASUREMENT_RE.match(text)
    if mixed:
        repaired = (
            f"{mixed.group('whole')} {mixed.group('num')}/{mixed.group('den')} "
            f"{mixed.group('unit')}"
        )
        return repaired + text[mixed.end():]

    simple = _SLASHLESS_SIMPLE_FRACTION_MEASUREMENT_RE.match(text)
    if simple:
        repaired = f"{simple.group('num')}/{simple.group('den')} {simple.group('unit')}"
        return repaired + text[simple.end():]

    mixed_mass = _SLASHLESS_MIXED_FRACTION_MASS_MEASUREMENT_RE.match(text)
    if mixed_mass:
        repaired = (
            f"{mixed_mass.group('whole')} {mixed_mass.group('num')}/{mixed_mass.group('den')} "
            f"{mixed_mass.group('unit')}"
        )
        return repaired + text[mixed_mass.end():]

    simple_mass = _SLASHLESS_SIMPLE_FRACTION_MASS_MEASUREMENT_RE.match(text)
    if simple_mass:
        repaired = (
            f"{simple_mass.group('num')}/{simple_mass.group('den')} "
            f"{simple_mass.group('unit')}"
        )
        return repaired + text[simple_mass.end():]
    return text


def _extract_leading_measurement_from_name(
    name: str,
) -> Optional[Tuple[str, str, str]]:
    """Detect a measurement fused into the start of an ingredient name.

    Returns (qty, unit, stripped_name) when the name begins with something
    like "12 lbs lean hamburger" or "1/2 cup sugar". Returns None otherwise.
    Slashless artifacts ("12 lbs") are repaired before extraction.
    """
    if not name:
        return None
    repaired = _repair_slashless_fraction_measurement(
        _normalize_fraction_text(str(name).strip())
    )
    match = _NAME_LEADING_MEASUREMENT_RE.match(repaired)
    if not match:
        return None
    qty = match.group("qty").strip()
    unit = _clean_unit(match.group("unit") or "")
    if unit is None:
        return None
    rest = match.group("rest").strip()
    if not rest:
        return None
    return qty, unit, rest


def _extract_leading_unit_from_name(name: str) -> Optional[Tuple[str, str]]:
    """Detect a bare unit abbreviation fused into the start of an ingredient name.

    Returns (normalized_unit, stripped_name) for names like
    "c. grated Parmesan cheese" or "dashes Salt". Returns None otherwise.
    """
    if not name:
        return None
    match = _NAME_LEADING_UNIT_RE.match(str(name).strip())
    if not match:
        return None
    unit = _NAME_LEADING_UNIT_MAP.get(match.group("unit").lower())
    if not unit:
        return None
    rest = re.sub(r"^(?:of|the)\b[\s,]*", "", match.group("rest").strip(), flags=re.IGNORECASE).strip(" ,")
    if not rest:
        return None
    return unit, rest


def _parse_word_quantity(text: str) -> Tuple[Optional[float], str, bool]:
    tokens = text.split()
    if not tokens:
        return None, text, False
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""
    if first == "half" and second in {"a", "an"}:
        return 0.5, " ".join(tokens[2:]), True
    if first in {"a", "an"} and second == "half":
        return 0.5, " ".join(tokens[2:]), True
    if first in _WORD_QUANTITIES:
        return _WORD_QUANTITIES[first], " ".join(tokens[1:]), True
    return None, text, False


def _split_measurement(measurement: Any) -> Tuple[Optional[str], Optional[str], bool]:
    if measurement is None:
        return None, None, False
    if isinstance(measurement, (int, float)):
        return str(measurement), None, False
    text = _repair_slashless_fraction_measurement(
        _normalize_fraction_text(str(measurement).strip().lower())
    )
    if not text:
        return None, None, False

    multiplier_mass = _MULTIPLIER_MASS_MEASUREMENT_RE.match(text)
    if multiplier_mass:
        count = float(multiplier_mass.group("count"))
        size = float(multiplier_mass.group("size"))
        unit = _clean_unit(multiplier_mass.group("unit") or "")
        return str(count * size), unit, False

    # "28 oz can" → 1 can (the 28 oz is the package size, not a count). The
    # explicit-package-size path re-reads "28 oz" from the raw measurement.
    package_prefix = _PACKAGE_SIZE_PREFIX_MEASUREMENT_RE.match(text)
    if package_prefix:
        return "1", _clean_unit(package_prefix.group("container") or ""), True

    # Recipe1M commonly stores mixed numbers as "1- 1/2 cup". Treat that
    # as 1 1/2, not a range from 1 to 1/2.
    mixed_match = _SPLIT_MIXED_NUMBER_RE.match(text)
    if mixed_match:
        text = (
            f"{mixed_match.group('whole')} {mixed_match.group('fraction')}"
            f"{mixed_match.group('rest') or ''}"
        ).strip()

    # Handle quantity ranges like "1/4 to 1/2 teaspoon ...".
    range_match = _RANGE_MEASUREMENT_RE.match(text)
    if range_match:
        q1 = _parse_quantity_value(range_match.group("q1"))
        q2 = _parse_quantity_value(range_match.group("q2"))
        if q1 is not None and q2 is not None:
            qty = str((q1 + q2) / 2.0)
            unit = _clean_unit(range_match.group("rest") or "")
            return qty, unit, False

    if not re.search(r"\d", text):
        qty_word, remainder, inferred = _parse_word_quantity(text)
        if qty_word is None:
            return None, None, False
        unit = _clean_unit(remainder or "")
        return str(qty_word), unit, inferred

    match = _MEASUREMENT_RE.match(text)
    if not match:
        return None, None, False
    qty = match.group("qty").strip()
    unit = _clean_unit(match.group("unit") or "")
    return qty, unit, False


def _infer_unit_from_name(name: str, bare_count: bool = True) -> Optional[str]:
    """``bare_count`` is False for a line with NO amount: only a bare count ("4 baguettes") may take a reviewed default unit."""
    name_norm = str(name).strip().lower()
    tokens = re.split(r"[\s,-]+", name_norm)
    if "rice paper" in name_norm:
        return "sheet"
    if "spray" in tokens and ("oil" in tokens or "cooking" in tokens):
        return "spray"
    if "oil" in tokens:
        # "olive oil" / "sesame oil": the countable noun is a modifier, not the item.
        return None
    if bare_count and "whole" in tokens and set(tokens) & {"chicken", "duck"} and not set(tokens) & _BIRD_PART_TOKENS:
        return "whole"
    if bare_count and set(tokens) & {"lettuce", "lettuces"} and set(tokens) & {"iceberg", "cos", "romaine"} and not set(tokens) & {"leaf", "leaves", "cup", "cups"}:
        return "head"
    if bare_count and set(tokens) & {"turnip", "turnips"}:
        return "whole"
    if bare_count and set(tokens) & {"brussels", "brussel"} and set(tokens) & {"sprout", "sprouts"}:
        return "sprout"
    if bare_count and set(tokens) & {"portobello", "portabella", "portabello"}:
        return "piece"
    if "fennel" in tokens:
        return "bulb"
    if "lettuce" in tokens and set(tokens) & {"leaf", "leaves", "cup", "cups"}:
        return "leaf"
    if "star" in tokens and "anise" in tokens:
        return "whole"
    if "vanilla" in tokens and ("bean" in tokens or "pod" in tokens):
        return "pod"
    if "weet-bix" in name_norm or "weet bix" in name_norm:
        return "whole"
    if "bouquet garni" in name_norm:
        return "whole"
    if "ginger" in tokens:
        return "piece"
    if "lemongrass" in tokens or "lemon grass" in name_norm:
        return "stalk"
    if "cinnamon" in tokens:
        return "stick"
    preferred_units = {
        "leaf",
        "leaves",
        "stalk",
        "stalks",
        "slice",
        "slices",
        "sheet",
        "sheets",
        "shell",
        "shells",
        "cube",
        "cubes",
        "bulb",
        "bulbs",
        "breast",
        "breasts",
        "fillet",
        "fillets",
        "filet",
        "filets",
        "muffin",
        "muffins",
        "bagel",
        "bagels",
        "head",
        "heads",
        "bun",
        "buns",
        "cake",
        "cakes",
    }
    for token in tokens:
        if token in preferred_units and token in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[token]
    for token in tokens:
        if token in _COUNTABLE_NOUNS and token not in {"whole", "wholes"}:
            return _COUNTABLE_NOUNS[token]
    return reviewed_default_unit(name_norm) if bare_count and REVIEWED_TABLES_ENABLED else None


def _parse_quantity_value(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = _normalize_fraction_text(str(value)).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if " " in text:
        whole, frac = text.split(" ", 1)
        try:
            return float(whole) + _parse_quantity_value(frac)
        except (TypeError, ValueError):
            return None
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            return float(num) / float(den)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _is_large_bare_number_quantity(qty: Optional[str]) -> bool:
    qty_value = _parse_quantity_value(qty)
    return qty_value is not None and qty_value >= LARGE_BARE_NUMBER_GRAMS_THRESHOLD


def _common_unit_reference_grams(
    name: str,
    unit: Optional[str],
    measurement: Any = None,
) -> Optional[tuple[float, str]]:
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None

    name_norm = _normalize_lookup_ingredient(name)
    tokens = _name_tokens(name_norm)
    tokens = tokens | {_singularize_token(token) for token in tokens}
    if unit_norm in set(_COUNTABLE_NOUNS.values()):
        # The parsed count noun is often more reliably singular than the
        # lightweight name singularizer (for example limes -> lime).
        tokens.add(unit_norm)
    measurement_tokens = _name_tokens(str(measurement or ""))
    size_hint = next(
        (size for size in ("small", "medium", "large") if size in measurement_tokens or size in tokens),
        None,
    )

    if unit_norm == "stick" and "butter" in tokens:
        return 113.0, "butter stick"
    if unit_norm == "clove" and "garlic" in tokens:
        return 3.0, "garlic clove"
    if unit_norm == "clove" and tokens & {"clove", "cloves"}:
        return 0.2, "whole spice clove"
    if unit_norm == "celery" and "celery" in tokens:
        return 40.0, "celery stalk"
    if unit_norm == "egg" and ("egg" in tokens or "eggs" in tokens):
        if "white" in tokens or "whites" in tokens:
            return 33.0, "large egg white"
        if "yolk" in tokens or "yolks" in tokens:
            return 18.0, "large egg yolk"
        return 50.0, "large egg"
    if unit_norm == "egg_white" and ("egg" in tokens or "white" in tokens or "whites" in tokens):
        return 33.0, "large egg white"
    if unit_norm == "egg_yolk" and ("egg" in tokens or "yolk" in tokens or "yolks" in tokens):
        return 18.0, "large egg yolk"
    if unit_norm == "pinch" and (
        tokens & PEPPER_PINCH_TOKENS
        or _is_pinch_default_candidate(name, None, _has_herb_spice_hint(name))
        or tokens & {"sugar", "salt"}
    ):
        return HERB_MISSING_UNIT_FALLBACK_GRAMS, "pinch"
    if unit_norm == "dash" and _is_pinch_default_candidate(name, None, False):
        return _VOLUME_UNIT_ML["dash"], "seasoning dash"
    if unit_norm == "packet" and (tokens & GELATIN_PACKET_TOKENS or "gelatin" in name_norm):
        return 7.0, "gelatin packet"
    if unit_norm == "envelope" and (tokens & HERB_SPICE_CLASS_TOKENS or "sazon" in tokens or "seasoning" in tokens):
        return 5.0, "seasoning envelope"
    if unit_norm in {"bag", "packet"} and "tea" in tokens:
        return 2.0, "tea bag"
    if unit_norm == "cake" and "yeast" in tokens:
        return 18.0, "compressed yeast cake"
    if unit_norm == "cup" and "ice" in tokens and "water" not in tokens:
        return 140.0, "ice cubes cup"
    if unit_norm in {"whole", "piece"} and "cube" in tokens and "ice" in tokens:
        return 22.0, "ice cube"
    if unit_norm == "leaf" and tokens & HERB_LEAF_TOKENS:
        return 0.5, "herb leaf"
    if unit_norm == "leaf" and "lettuce" in tokens:
        return 10.0, "lettuce leaf"
    if (
        unit_norm == "bunch"
        and (
            tokens & {"scallion", "scallions"}
            or "green onion" in name_norm
            or "green onions" in name_norm
            or "spring onion" in name_norm
            or "spring onions" in name_norm
        )
    ):
        return 100.0, "scallion/green onion bunch"
    if (
        unit_norm in {"whole", "piece"}
        and (
            tokens & {"scallion", "scallions"}
            or "green onion" in name_norm
            or "green onions" in name_norm
            or "spring onion" in name_norm
            or "spring onions" in name_norm
        )
    ):
        return 15.0, "scallion/green onion"
    if unit_norm == "bunch" and tokens & LEAFY_GREEN_BUNCH_TOKENS:
        return 250.0, "leafy green bunch"
    if unit_norm == "bunch" and tokens & SOFT_HERB_BUNCH_TOKENS:
        return 60.0, "soft herb bunch"
    if unit_norm == "bunch" and tokens & SPRIG_HERB_TOKENS:
        return 30.0, "herb bunch"
    if unit_norm == "bunch" and "asparagus" in tokens:
        return 450.0, "asparagus bunch"
    if unit_norm == "bunch" and "broccolini" in tokens:
        return 200.0, "broccolini bunch"
    if unit_norm == "bunch" and tokens & {"bok", "choi", "choy", "pak"}:
        return 300.0, "Asian greens bunch"
    if unit_norm == "bunch" and "watercress" in tokens:
        return 250.0, "watercress bunch"
    if unit_norm == "bunch" and "chives" in tokens:
        return 50.0, "chives bunch"
    if unit_norm == "bunch" and ("silver beet" in name_norm or "silverbeet" in tokens):
        return 250.0, "silverbeet bunch"
    if unit_norm == "bunch" and tokens & {"radish", "radishes"}:
        return 200.0, "radish bunch"
    if unit_norm == "sprig" and tokens & SPRIG_HERB_TOKENS:
        return 1.0, "herb sprig"
    single_count_unit = (
        unit_norm in {"whole", "piece"}
        or unit_norm in _SIZE_WORDS
        or unit_norm
        in {
            "apple",
            "banana",
            "lemon",
            "lime",
            "orange",
            "carrot",
            "onion",
            "potato",
            "pepper",
            "zucchini",
            "squash",
            "avocado",
            "tomato",
            "peach",
            "shallot",
            "jalapeno",
            "chile",
            "pear",
            "strawberry",
            "kiwi",
            "plum",
            "apricot",
            "prune",
            "fig",
            "grapefruit",
            "mandarin",
            "pomegranate",
            "date",
            "olive",
            "grape",
            "raisin",
            "tortilla",
            "wrap",
            "pita",
            "roll",
            "thigh",
            "drumstick",
            "shank",
            "cutlet",
            "tenderloin",
            "scallop",
            "base",
            "finger",
            "biscuit",
            "scallion",
            "passionfruit",
            "nectarine",
            "swede",
            "kumara",
            "gherkin",
            "naan",
            "roti",
            "cracker",
            "oatcake",
        }
    )
    size_factor = _SIZE_FACTORS.get(unit_norm, _SIZE_FACTORS.get(size_hint or "", 1.0))
    def sized(grams: float, label: str) -> tuple[float, str]:
        if unit_norm in _SIZE_WORDS:
            return grams * size_factor, f"{unit_norm} {label}"
        return grams, label

    if unit_norm in {"whole", "piece", "stalk", "cap"} and tokens & {"portobello", "portabella", "portabello"}:
        return 84.0, "portobello cap (USDA)"
    if single_count_unit and "lettuce" in tokens and tokens & {"leaf", "leaves"}:
        return sized(10.0, "lettuce leaf")
    if unit_norm in {"whole", "egg"} and "egg" in tokens:
        return 50.0, "large egg"
    if single_count_unit and "apple" in tokens:
        return sized(182.0, "apple")
    if single_count_unit and "banana" in tokens:
        return sized(118.0, "banana")
    if single_count_unit and tokens & {"mango", "mangos", "mangoes"}:
        return sized(200.0, "mango")
    if single_count_unit and "lemon" in tokens:
        return sized(58.0, "lemon")
    if single_count_unit and "lime" in tokens:
        return sized(67.0, "lime")
    if single_count_unit and "orange" in tokens:
        return sized(131.0, "orange")
    if single_count_unit and "carrot" in tokens and "baby" in tokens:
        return sized(12.0, "baby carrot")
    if single_count_unit and "carrot" in tokens:
        return sized(61.0, "carrot")
    if single_count_unit and "onion" in tokens and (tokens & {"spring", "green", "scallion", "scallions"}):
        return sized(15.0, "spring/green onion")
    if single_count_unit and "onion" in tokens and (tokens & {"pearl", "baby", "pickling", "silverskin"}):
        return sized(10.0, "pearl onion")
    if single_count_unit and "onion" in tokens:
        return sized(110.0, "onion")
    if single_count_unit and "potato" in tokens and (tokens & {"baby", "new"}):
        return sized(25.0, "baby/new potato")
    if single_count_unit and "potato" in tokens:
        return sized(173.0, "potato")
    if single_count_unit and "avocado" in tokens:
        return sized(150.0, "avocado")
    if single_count_unit and "tomato" in tokens and (tokens & {"cherry", "grape"}):
        return sized(17.0, "cherry/grape tomato")
    if single_count_unit and "tomato" in tokens and (tokens & {"plum", "roma"}):
        return sized(62.0, "plum tomato")
    if single_count_unit and "tomato" in tokens and tokens & {"dried", "sundried"}:
        return sized(3.0, "USDA oil-packed sun-dried tomato")
    if single_count_unit and "tomato" in tokens:
        return sized(123.0, "tomato")
    if single_count_unit and "peach" in tokens:
        return sized(150.0, "peach")
    if single_count_unit and "pineapple" in tokens:
        return sized(905.0, "pineapple edible portion")
    if single_count_unit and "pear" in tokens:
        return sized(178.0, "pear")
    if single_count_unit and "strawberry" in tokens:
        return sized(12.0, "strawberry")
    if single_count_unit and "kiwi" in tokens:
        return sized(69.0, "kiwifruit")
    if single_count_unit and "plum" in tokens:
        return sized(66.0, "plum")
    if single_count_unit and "apricot" in tokens:
        return sized(35.0, "apricot")
    if single_count_unit and "prune" in tokens:
        return sized(9.5, "prune")
    if single_count_unit and "fig" in tokens:
        return sized(50.0, "fig")
    if single_count_unit and "grapefruit" in tokens:
        return sized(230.0, "grapefruit")
    if single_count_unit and "mandarin" in tokens:
        return sized(88.0, "mandarin")
    if single_count_unit and "pomegranate" in tokens:
        return sized(282.0, "pomegranate")
    if single_count_unit and "date" in tokens:
        return sized(7.0, "date")
    if single_count_unit and "olive" in tokens:
        return sized(4.0, "olive")
    if single_count_unit and "grape" in tokens:
        return sized(5.0, "grape")
    if single_count_unit and "raisin" in tokens:
        return sized(0.5, "raisin")
    if single_count_unit and "passionfruit" in tokens:
        return sized(18.0, "passionfruit")
    if single_count_unit and "nectarine" in tokens:
        return sized(142.0, "nectarine")
    if single_count_unit and "swede" in tokens:
        return sized(800.0, "swede")
    if single_count_unit and "kumara" in tokens:
        return sized(200.0, "kumara")
    if single_count_unit and "scallion" in tokens:
        return sized(15.0, "scallion")
    if single_count_unit and "gherkin" in tokens:
        return sized(35.0, "gherkin")
    if single_count_unit and "shallot" in tokens:
        return sized(25.0, "shallot")
    if single_count_unit and "garlic" in tokens:
        return sized(3.0, "garlic clove")
    if single_count_unit and "celery" in tokens:
        return sized(40.0, "celery stalk")
    if single_count_unit and "chicken" in tokens and tokens & {"breast", "fillet"}:
        return sized(174.0, "chicken breast")
    if single_count_unit and "cabbage" in tokens:
        return sized(900.0, "cabbage head")
    if single_count_unit and "cucumber" in tokens:
        return sized(250.0, "cucumber")
    if single_count_unit and tokens & {"radish", "radishes"}:
        return sized(15.0, "radish")
    if single_count_unit and "fennel" in tokens:
        return sized(234.0, "fennel bulb")
    if single_count_unit and "spinach" in tokens:
        return sized(250.0, "spinach bunch")
    if single_count_unit and "egg" in tokens and not tokens & {
        "yolk",
        "yolks",
        "white",
        "whites",
    }:
        return sized(50.0, "egg")
    if single_count_unit and "papaya" in tokens:
        return sized(500.0, "papaya edible portion")
    if single_count_unit and "pumpkin" in tokens:
        return sized(1000.0, "pumpkin")
    if single_count_unit and "melon" in tokens:
        return sized(1000.0, "melon")
    if single_count_unit and tokens & {
        "arbol", "morita", "chipotle", "guajillo", "ancho", "pasilla",
    }:
        return sized(2.0, "dried chile pepper")
    if single_count_unit and (tokens & {"jalapeno", "chile", "chilli", "chili"}):
        return sized(14.0, "jalapeno chile")
    if (
        single_count_unit
        and ("zucchini" in tokens or "squash" in tokens)
        and not tokens & {"blossom", "blossoms", "flower", "flowers"}
    ):
        return sized(196.0, "summer squash")
    if (
        single_count_unit
        and "dried" in tokens
        and tokens & {"pepper", "chile", "chilli", "chili"}
    ):
        return sized(2.0, "dried chile pepper")
    if single_count_unit and tokens & {"pepper", "capsicum"}:
        return sized(120.0, "bell pepper")
    if single_count_unit and tokens & {"eggplant", "aubergine"}:
        return sized(458.0, "eggplant/aubergine")
    if single_count_unit and "beetroot" in tokens:
        return sized(82.0, "beetroot")
    if single_count_unit and "parsnip" in tokens:
        return sized(120.0, "parsnip")
    if single_count_unit and "mushroom" in tokens:
        return sized(18.0, "mushroom")
    if single_count_unit and "leek" in tokens:
        return sized(89.0, "leek")
    if single_count_unit and "fennel" in tokens and tokens & {"bulb", "bulbs"}:
        return sized(234.0, "fennel bulb")
    if single_count_unit and tokens & {"courgette", "courgettes"}:
        return sized(196.0, "courgette")
    if single_count_unit and "egg" in tokens and tokens & {"yolk", "yolks"}:
        return sized(18.0, "egg yolk")
    if single_count_unit and "egg" in tokens and tokens & {"white", "whites"}:
        return sized(33.0, "egg white")
    if single_count_unit and tokens & {"prawn", "prawns", "shrimp"}:
        return sized(20.0, "large prawn")
    if single_count_unit and "celery" in tokens and tokens & {"stalk", "stalks"}:
        return sized(40.0, "celery stalk")
    if single_count_unit and tokens & {"wrap", "wraps", "tortilla", "tortillas"}:
        return sized(50.0, "tortilla/wrap")
    if single_count_unit and tokens & {"pita", "pitta", "pitas", "pittas"}:
        return sized(60.0, "pita bread")
    if single_count_unit and "roll" in tokens:
        return sized(60.0, "bread roll")
    if single_count_unit and "lamb" in tokens and tokens & {"cutlet", "chop"}:
        return sized(113.0, "lamb cutlet")
    if unit_norm == "thigh" and "chicken" in tokens:
        return 125.0, "chicken thigh"
    if unit_norm == "drumstick" and "chicken" in tokens:
        return 95.0, "chicken drumstick"
    if unit_norm == "shank" and "lamb" in tokens:
        return 340.0, "lamb shank"
    if unit_norm == "cutlet" and "lamb" in tokens:
        return 113.0, "lamb cutlet"
    if unit_norm == "tenderloin" and "chicken" in tokens:
        return 100.0, "chicken tenderloin"
    if unit_norm == "scallop" and "scallop" in tokens:
        return 30.0, "large scallop"
    if unit_norm == "cube" and "watermelon" in tokens:
        return 8.0, "2cm watermelon cube"
    if unit_norm == "cube" and ("honeydew" in tokens or "melon" in tokens):
        return 8.0, "2cm melon cube"
    if unit_norm == "cube" and tokens & {"stock", "bouillon"}:
        return 10.0, "stock cube"
    if unit_norm == "sheet" and "nori" in tokens:
        return 2.5, "nori sheet"
    if unit_norm == "sheet" and "rice" in tokens and "paper" in tokens:
        return 9.0, "rice-paper sheet"
    if unit_norm == "sheet" and tokens & {"puff", "shortcrust", "brik"}:
        return 150.0, "pastry sheet"
    if unit_norm == "stick" and "celery" in tokens:
        return 40.0, "celery stalk"
    if unit_norm == "stick" and "cinnamon" in tokens:
        return 3.0, "cinnamon stick"
    if unit_norm in {"stick", "stalk"} and (
        "lemongrass" in tokens or "lemon grass" in name_norm
    ):
        return 12.0, "lemongrass stalk"
    if unit_norm == "stalk" and "celery" in tokens:
        return 40.0, "celery stalk"
    if unit_norm == "stalk" and "broccoli" in tokens:
        return 151.0, "broccoli stalk"
    if unit_norm == "bulb" and "fennel" in tokens:
        return 234.0 * size_factor, f"{size_hint or 'medium'} fennel bulb"
    if unit_norm in {"head", "whole"} and "cauliflower" in tokens:
        return 575.0 * size_factor, f"{size_hint or 'medium'} cauliflower head"
    if unit_norm == "head" and "broccoli" in tokens:
        return 608.0 * size_factor, f"{size_hint or 'medium'} broccoli head"
    if unit_norm == "head" and "baby" in tokens and tokens & {"cos", "lettuce"}:
        return 163.0, "baby lettuce head"
    if unit_norm == "head" and "lettuce" in tokens and "iceberg" in tokens:
        return 539.0, "iceberg lettuce head (USDA medium)"
    if unit_norm == "head" and "lettuce" in tokens and tokens & {"cos", "romaine"}:
        return 626.0, "cos/romaine lettuce head (USDA)"
    if unit_norm == "head" and "lettuce" in tokens:
        return 500.0, "lettuce head"
    if unit_norm == "sprout" and tokens & {"brussels", "brussel"}:
        return 19.0, "brussels sprout (USDA)"
    if unit_norm in _TURNIP_SIZE_GRAMS and tokens & {"turnip", "turnips"}:
        return _TURNIP_SIZE_GRAMS[unit_norm], f"{unit_norm} turnip (USDA)"
    if single_count_unit and tokens & {"turnip", "turnips"}:
        return 122.0, "medium turnip (USDA)"
    if single_count_unit and "muffin" in tokens and "english" in tokens:
        return 57.0, "English muffin (USDA)"
    if unit_norm == "leaf" and "lettuce" in tokens:
        return 15.0, "lettuce leaf"
    if unit_norm == "bulb" and "garlic" in tokens:
        return 50.0, "garlic bulb"
    if unit_norm == "piece" and "ginger" in tokens:
        return 15.0, "thumb-sized ginger piece"
    if unit_norm == "thumb" and "ginger" in tokens:
        return 15.0, "thumb-sized ginger piece"
    if unit_norm == "inch" and "ginger" in tokens:
        return 7.5, "ginger inch"
    if unit_norm == "long" and tokens & {"chile", "chilli", "pepper"}:
        return 20.0, "long chile"
    if unit_norm == "rasher":
        return 30.0, "bacon rasher"
    if unit_norm == "cob" and tokens & {"corn", "sweetcorn"}:
        return 90.0, "corn cob kernels"
    if unit_norm == "cup" and tokens & {"frisee", "endive"}:
        return 50.0, "raw endive cup"
    if unit_norm == "cup" and (
        tokens & {"silverbeet", "chard"} or "silver beet" in name_norm
    ):
        return 36.0, "raw swiss chard cup"
    if unit_norm == "cup" and "microgreens" in tokens:
        return 20.0, "leafy microgreens cup"
    if unit_norm == "cup" and "orecchiette" in tokens:
        return 90.0, "dry small pasta cup"
    if unit_norm == "cup" and tokens & {"courgette", "zucchini"}:
        return 124.0, "raw zucchini cup"
    if unit_norm == "cup" and "rice" in tokens:
        return 185.0, "rice cup"
    if unit_norm == "cup" and "polenta" in tokens:
        return 160.0, "dry polenta cup"
    if unit_norm == "cup" and "mesclun" in tokens:
        return 20.0, "mesclun cup"
    if unit_norm == "cup" and "tzatziki" in tokens:
        return 240.0, "tzatziki cup"
    if unit_norm == "cup" and "broccolini" in tokens:
        return 88.0, "chopped broccolini cup"
    if unit_norm == "cup" and tokens & {"bok", "choi", "choy", "pak"}:
        return 70.0, "chopped Asian greens cup"
    if unit_norm == "cup" and "rocket" in tokens:
        return 20.0, "rocket/arugula cup"
    if unit_norm == "cup" and "bread" in tokens and tokens & {"cube", "cubes"}:
        return 35.0, "bread cubes cup"
    if unit_norm == "cup" and "apple" in tokens:
        return 125.0, "chopped apple cup"
    if unit_norm == "cup" and "pineapple" in tokens:
        return 165.0, "pineapple chunks cup"
    if (
        unit_norm == "cup"
        and "vegetable" in tokens
        and not tokens & {"stock", "broth", "bouillon"}
    ):
        if "leafy" in tokens:
            return 40.0, "loose leafy vegetables cup"
        if "frozen" in tokens:
            return 182.0, "frozen mixed vegetables cup"
        return 150.0, "mixed vegetables cup"
    if unit_norm == "breast" and tokens & POULTRY_TOKENS:
        return 174.0, "boneless chicken breast"
    if unit_norm == "fillet" and tokens & {"anchovy", "anchovies"}:
        return 5.0, "anchovy fillet"
    if unit_norm == "fillet" and tokens & FISH_SHELLFISH_TOKENS:
        return 120.0, "fish fillet"
    if unit_norm == "prawn" and ("prawn" in tokens or "shrimp" in tokens):
        return 20.0, "large prawn"
    if unit_norm == "chop" and tokens & {"lamb", "pork"}:
        return 113.0, "meat chop"
    if unit_norm == "sausage" and "sausage" in tokens:
        return 75.0, "sausage link"
    if unit_norm == "bun" and tokens & {"bun", "buns", "burger"}:
        return 60.0, "burger bun"
    if unit_norm == "bagel" and "bagel" in tokens:
        return 95.0, "bagel"
    if unit_norm == "muffin" and "english" in tokens:
        return 57.0, "english muffin"
    if unit_norm == "package" and "gelatin" in tokens:
        return 85.0, "gelatin dessert package"
    if unit_norm == "package" and "cream cheese" in name_norm:
        return 226.0, "cream cheese package"
    if unit_norm == "packet" and "yeast" in tokens:
        return 7.0, "yeast packet"
    if (
        (unit_norm == "sheet" or single_count_unit)
        and tokens & {"lasagna", "lasagne"}
        and tokens & {"sheet", "sheets"}
    ):
        return 21.0, "lasagna sheet"
    if (
        (unit_norm == "sheet" or single_count_unit)
        and tokens & {"filo", "phyllo"}
    ):
        return 18.0, "filo/phyllo sheet"
    if unit_norm == "can" and "tomato" in tokens:
        return 411.0, "standard tomato can"
    if unit_norm == "can" and ("bean" in tokens or "beans" in tokens):
        return 425.0, "standard bean can"
    if unit_norm == "can" and "pumpkin" in tokens and tokens & {"puree", "purée"}:
        return 425.0, "standard pumpkin puree can"
    if unit_norm == "jar" and ("sauce" in tokens or "salsa" in tokens):
        return 454.0, "standard sauce jar"
    if unit_norm in {"whole", "piece", "shell"} and "pizza" in tokens and "shell" in tokens:
        return 142.0, "thin pizza shell"
    if unit_norm == "shell" and "taco" in tokens:
        return 12.0, "taco shell"
    if unit_norm == "slice" and "cheese" in tokens:
        return 20.0, "cheese slice"
    if unit_norm == "slice" and tokens & {"bread", "toast", "sourdough"}:
        return 30.0, "bread slice"
    if unit_norm == "slice" and "cucumber" in tokens:
        return 7.0, "cucumber slice"
    if unit_norm == "slice" and "tomato" in tokens:
        return 20.0, "tomato slice"
    if unit_norm == "slice" and "ginger" in tokens:
        return 5.0, "ginger slice"
    if unit_norm == "slice" and tokens & {"prosciutto", "ham", "bacon"}:
        return 15.0, "cured-meat slice"
    if (
        unit_norm == "cup"
        and "salad" in tokens
        and not tokens & _PREPARED_SALAD_TOKENS
        and not {"dressing", "sauce"} & tokens
    ):
        # One cup of loose mixed salad leaves is roughly 30--50 g. Without a
        # specific reference, vector lookup latched onto the word "dressing"
        # and treated eight cups of salad as 2.3 kg of mayonnaise.
        return 40.0, "loose mixed salad cup"
    if unit_norm == "cup" and tokens & {"slaw", "coleslaw"}:
        return 70.0, "shredded slaw cup"
    if unit_norm in _VOLUME_UNIT_ML and tokens & _FRESH_HERB_TOKENS:
        grams_per_cup = 25.0
        return grams_per_cup * _VOLUME_UNIT_ML[unit_norm] / 240.0, "fresh herb volume"
    if unit_norm in _VOLUME_UNIT_ML and _has_herb_spice_hint(name):
        return 0.4 * _VOLUME_UNIT_ML[unit_norm], "dry seasoning volume"
    if unit_norm in _VOLUME_UNIT_ML and "ginger" in tokens:
        return 0.4 * _VOLUME_UNIT_ML[unit_norm], "minced ginger volume"
    if unit_norm in _VOLUME_UNIT_ML and "garlic" in tokens:
        return 0.6 * _VOLUME_UNIT_ML[unit_norm], "minced garlic volume"
    if unit_norm in _VOLUME_UNIT_ML and "parmesan" in tokens:
        return 0.47 * _VOLUME_UNIT_ML[unit_norm], "grated parmesan volume"
    if unit_norm in _VOLUME_UNIT_ML and "rice" in tokens:
        return (185.0 / 240.0) * _VOLUME_UNIT_ML[unit_norm], "rice volume"
    if unit_norm in _VOLUME_UNIT_ML and "sweetcorn" in tokens:
        return (164.0 / 240.0) * _VOLUME_UNIT_ML[unit_norm], "sweetcorn volume"
    if unit_norm in _VOLUME_UNIT_ML and tokens & {"sultana", "sultanas", "raisin", "raisins"}:
        grams_per_cup = 165.0
        return grams_per_cup * _VOLUME_UNIT_ML[unit_norm] / 240.0, "dried fruit volume"
    if unit_norm == "spray" and tokens & {"oil", "oils"}:
        return 1.0, "oil spray"
    if unit_norm == "spray" and tokens & {"cooking", "spray"}:
        return 1.0, "cooking spray"
    if unit_norm == "handful" and tokens & _FRESH_HERB_TOKENS:
        return 15.0, "fresh-herb handful"
    if unit_norm == "handful" and tokens & {"spinach", "rocket", "watercress", "greens"}:
        return 30.0, "leafy-green handful"
    if unit_norm == "handful" and tokens & {
        "almond", "almonds", "cashew", "cashews", "peanut", "peanuts",
        "walnut", "walnuts", "pinenut", "pinenuts", "nut", "nuts",
        "seed", "seeds",
    }:
        return 30.0, "nuts/seeds handful"
    if unit_norm == "handful" and tokens & {"raisin", "raisins", "sultana", "sultanas"}:
        return 30.0, "dried-fruit handful"
    if unit_norm == "handful" and tokens & {"berry", "berries", "blueberry", "blueberries"}:
        return 80.0, "berry handful"
    if unit_norm == "handful" and tokens & {"herb", "herbs", "curry", "sprout", "sprouts"}:
        return 15.0, "loose herbs/sprouts handful"
    if unit_norm == "spear" and "asparagus" in tokens:
        return 16.0, "asparagus spear"
    if unit_norm == "leaf" and tokens & {"kaffir", "makrut"}:
        return 0.3, "makrut lime leaf"
    if single_count_unit and "brussels" in tokens and tokens & {"sprout", "sprouts"}:
        return sized(19.0, "Brussels sprout")
    if unit_norm == "sprinkling" and (
        _has_herb_spice_hint(name) or tokens & {"salt", "sugar", "seasoning"}
    ):
        return 0.5, "seasoning sprinkling"
    if unit_norm in {"crack", "grind"} and tokens & {"pepper", "peppercorn", "peppercorns"}:
        return 0.1, "pepper mill turn"
    if unit_norm == "few" and tokens & {"peppercorn", "peppercorns"}:
        return 0.3, "few peppercorns"
    if unit_norm == "knob" and "butter" in tokens:
        return 10.0, "butter knob"
    if unit_norm == "squeeze" and tokens & {"lemon", "lime", "juice"}:
        return 15.0, "citrus squeeze"
    if unit_norm == "zest" and tokens & {"lemon", "lime", "orange"}:
        return 6.0, "citrus zest"
    if unit_norm == "cm" and "ginger" in tokens:
        return 3.0, "ginger centimetre"
    if unit_norm == "cm" and "cucumber" in tokens:
        return 14.3, "USDA whole-cucumber length-derived centimetre"
    if unit_norm == "punnet" and tokens & {"tomato", "strawberry", "berry"}:
        return 250.0, "produce punnet"
    if unit_norm == "pod" and "vanilla" in tokens:
        return 5.0, "vanilla pod"
    if unit_norm == "pod" and "cardamom" in tokens:
        return 0.2, "cardamom pod"
    if unit_norm == "wedge" and tokens & {"lemon", "lime", "orange"}:
        return 7.0, "citrus wedge"
    if unit_norm == "spear" and "pineapple" in tokens:
        return 28.0, "pineapple spear"
    if unit_norm == "wedge" and "pumpkin" in tokens:
        return 200.0, "pumpkin wedge"
    if unit_norm == "stalk" and "rhubarb" in tokens:
        return 50.0, "rhubarb stalk"
    if unit_norm == "tray" and "ice" in tokens:
        return 300.0, "ice-cube tray"
    if unit_norm == "bunch" and "grape" in tokens:
        return 500.0, "grape bunch"
    if unit_norm == "head" and "garlic" in tokens:
        return 50.0, "garlic bulb"
    if unit_norm in {"tablespoon", "tbsp"} and "lard" in tokens:
        return 13.0, "lard tablespoon"
    if unit_norm == "greasing" and "oil" in tokens:
        # Greasing a tin/pan: almost none is eaten. Policy value; the sodium/kcal test showed the
        # 0-10 g range changes recipe error by <1%, so 2 g is a flagged placeholder, not a calibrated number.
        return GREASING_OIL_GRAMS, "oil for greasing (policy)"
    if unit_norm == "pinch" and "saffron" in tokens:
        return 0.1, "saffron pinch"
    if unit_norm == "whole" and "star" in tokens and "anise" in tokens:
        return 2.0, "star anise"
    if unit_norm == "whole" and tokens & {"weet", "bix"}:
        return 15.0, "Weet-Bix biscuit"
    if unit_norm == "whole" and "bouquet" in tokens and "garni" in tokens:
        return 5.0, "bouquet garni"
    if unit_norm == "whole" and "duck" in tokens and not tokens & _BIRD_PART_TOKENS:
        # USDA: 0.5 duck, bone removed = 634 g meat+skin at 0.633 yield -> ~2.0 kg ready-to-cook.
        return 2000.0, "whole duck"
    if unit_norm == "whole" and "chicken" in tokens and tokens & {"leg", "legs"}:
        return 200.0, "chicken leg"
    if (
        unit_norm in {"whole", "size"}
        and "chicken" in tokens
        and not tokens & {"leg", "legs", "thigh", "thighs", "breast", "breasts"}
    ):
        if "size 16" in str(measurement or "").lower():
            return 1600.0, "size-16 whole chicken"
        return 1600.0, "whole chicken"
    if unit_norm in {"teaspoon", "tsp"} and "bread" in tokens and "soda" in tokens:
        return 4.6, "bread/baking soda teaspoon"
    if single_count_unit and "pizza" in tokens and tokens & {"base", "bases"}:
        return sized(200.0, "pizza base")
    if single_count_unit and "sponge" in tokens and tokens & {"finger", "fingers"}:
        return sized(10.0, "sponge finger")
    if single_count_unit and "biscuit" in tokens:
        return sized(12.0, "biscuit")
    if single_count_unit and "naan" in tokens:
        return sized(90.0, "naan bread")
    if single_count_unit and "roti" in tokens:
        return sized(60.0, "roti")
    if single_count_unit and "cracker" in tokens:
        return sized(15.0, "cracker")
    if single_count_unit and "oatcake" in tokens:
        return sized(15.0, "oatcake")
    if unit_norm == "can" and "coconut" in tokens and "milk" in tokens:
        return 400.0, "coconut-milk can"
    if (
        unit_norm == "cup"
        and "cheese" in tokens
        and tokens & HARD_CHEESE_VOLUME_TOKENS
        and not any(phrase in name_norm for phrase in SOFT_CHEESE_EXCLUDED_PHRASES)
    ):
        return 113.0, "shredded hard cheese cup"
    if (
        unit_norm == "tablespoon"
        and "cheese" in tokens
        and tokens & HARD_CHEESE_VOLUME_TOKENS
        and not any(phrase in name_norm for phrase in SOFT_CHEESE_EXCLUDED_PHRASES)
    ):
        return 7.0, "shredded hard cheese tablespoon"
    return None


def _reference_weight_fallback(
    name: str,
    qty: Optional[str],
    unit: Optional[str],
    food_group: Optional[str] = None,
    canonical: Optional[str] = None,
    measurement: Any = None,
) -> Optional[tuple[float, dict, str]]:
    qty_value = _parse_quantity_value(qty)
    if qty_value is None:
        return None

    name_norm = str(name or "").strip().lower()
    name_tokens = set(re.findall(r"[a-z]+", name_norm))
    unit_norm = str(unit or "").strip().lower() or None

    if unit_norm in GENERIC_LLM_PACKAGE_UNITS:
        package_grams = _explicit_package_size_grams(measurement, name, canonical)
        if package_grams is not None:
            return (
                qty_value * package_grams,
                {
                    "portion_desc": f"{unit_norm} (explicit package size fallback)",
                    "grams_per_unit": package_grams,
                    "source": "explicit_package_size",
                },
                "explicit_package_size_fallback",
            )

    reviewed = reviewed_item_grams(name, unit_norm, qty_value, measurement) if REVIEWED_TABLES_ENABLED else None
    common_reference = _common_unit_reference_grams(name, unit_norm, measurement=measurement)
    if common_reference is None and reviewed is not None:
        grams_per_unit, label = reviewed
        return (
            qty_value * grams_per_unit,
            {
                "portion_desc": f"{unit_norm or 'each'} ({label})",
                "grams_per_unit": grams_per_unit,
                "source": "reviewed_item_weight",
            },
            "reviewed_item_weight_fallback",
        )
    if common_reference is not None:
        grams_per_unit, label = common_reference
        return (
            qty_value * grams_per_unit,
            {
                "portion_desc": f"{unit_norm} ({label} reference fallback)",
                "grams_per_unit": grams_per_unit,
                "source": "common_unit_reference",
            },
            "common_unit_reference_fallback",
        )

    density = _liquid_density_for_name(name, food_group=food_group, canonical=canonical)
    if density is not None and unit_norm in _VOLUME_UNIT_ML:
        density_g_per_ml, density_label = density
        unit_ml = _VOLUME_UNIT_ML[unit_norm]
        grams_per_unit = unit_ml * density_g_per_ml
        match_type = (
            "water_like_volume_fallback"
            if density_label == "water-like"
            else "liquid_density_volume_fallback"
        )
        return (
            qty_value * grams_per_unit,
            {
                "portion_desc": f"{unit_norm} ({density_label} density fallback)",
                "grams_per_unit": grams_per_unit,
                "density_g_per_ml": density_g_per_ml,
                "unit_ml": unit_ml,
            },
            match_type,
        )

    # USDA often has a reviewed cup weight but no separate ml/tablespoon
    # portion. Reuse that physical cup reference for foods without a more
    # specific liquid density above.
    if unit_norm in _VOLUME_UNIT_ML:
        volume_reference = find_weight_match_by_name(name=name, unit="cup")
        if volume_reference is None:
            aliased_name = _usda_query_alias(name)
            if aliased_name != str(name).strip().lower():
                volume_reference = find_weight_match_by_name(
                    name=aliased_name,
                    unit="cup",
                )
        portion = (volume_reference or {}).get("portion") or {}
        try:
            grams_per_cup = float(portion["grams_per_unit"])
        except (KeyError, TypeError, ValueError):
            grams_per_cup = 0.0
        if grams_per_cup > 0:
            grams_per_unit = grams_per_cup * _VOLUME_UNIT_ML[unit_norm] / 240.0
            return (
                qty_value * grams_per_unit,
                {
                    "portion_desc": f"{unit_norm} (USDA cup-density fallback)",
                    "grams_per_unit": grams_per_unit,
                    "source_food_name": volume_reference.get("food_name"),
                    "source": "usda_cup_density",
                },
                "usda_cup_density_fallback",
            )

    # Gap filler only: audited USDA cup weights above win over this reviewed table.
    if REVIEWED_TABLES_ENABLED and unit_norm in _VOLUME_UNIT_ML:
        for pattern, grams_per_cup, label in _REVIEWED_CUP_GRAMS:
            if pattern.search(name_norm):
                grams_per_unit = grams_per_cup * _VOLUME_UNIT_ML[unit_norm] / 240.0
                return (
                    qty_value * grams_per_unit,
                    {
                        "portion_desc": f"{unit_norm} ({label})",
                        "grams_per_unit": grams_per_unit,
                        "source": "reviewed_cup_density",
                    },
                    "reviewed_cup_density_fallback",
                )

    if REVIEWED_TABLES_ENABLED and unit_norm in _GENERIC_SPOON_GRAMS:
        return (
            qty_value * _GENERIC_SPOON_GRAMS[unit_norm],
            {
                "portion_desc": f"{unit_norm} (generic spoon convention)",
                "grams_per_unit": _GENERIC_SPOON_GRAMS[unit_norm],
                "source": "generic_spoon_convention",
            },
            "generic_spoon_convention",
        )

    if unit_norm in _SIZE_WORDS:
        reference = _lookup_llm_unit_grams(name)
        if reference is not None:
            grams_per_unit = float(reference["grams_per_unit"])
            factor = _SIZE_FACTORS.get(unit_norm, 1.0)
            return (
                qty_value * grams_per_unit * factor,
                {
                    "portion_desc": f"{unit_norm} (size-adjusted reference unit fallback)",
                    "grams_per_unit": grams_per_unit * factor,
                    "reference_unit": reference.get("unit"),
                    "reference_source": reference.get("source"),
                    "reference_ingredient": reference.get("ingredient"),
                },
                "size_adjusted_reference_unit_fallback",
            )

    return None


def _estimate_grams_from_usda_id(
    name: str,
    qty: str,
    unit: str,
    usda_id: str,
) -> tuple[float, Optional[dict], str]:
    unit_norm = unit.strip().lower()
    if unit_norm in _MASS_UNITS:
        qty_value = _parse_quantity_value(qty)
        if qty_value is None:
            raise ValueError("Invalid quantity value")
        grams = qty_value * _MASS_UNITS[unit_norm]
        return grams, None, "direct_mass"

    portion_match = match_portion(usda_id, unit, name=name)
    if not portion_match:
        density_result = weight_from_density_fallback(
            usda_id=usda_id,
            unit=unit,
            quantity=qty,
            name=name,
        )
        if not density_result:
            raise ValueError("No direct portion match")

        portion_match = {
            "portion_desc": f"{density_result['target_unit']} (density fallback)",
            "grams_per_unit": density_result["grams_per_target_unit"],
            "density_g_per_ml": density_result["density_g_per_ml"],
            "density_candidate_count": density_result["density_candidate_count"],
            "density_source_portion_desc": density_result["source_portion_desc"],
            "density_source_unit": density_result["source_unit"],
            "density_source_unit_ml": density_result["source_unit_ml"],
            "density_source_grams_per_unit": density_result["source_grams_per_unit"],
            "target_unit_ml": density_result["target_unit_ml"],
        }
        return float(density_result["grams"]), portion_match, "density_fallback"

    grams = weight_from_ingredient(
        {"name": name, "usda_id": usda_id, "quantity": qty, "unit": unit}
    )
    return grams, portion_match, "direct"


def _estimate_grams_from_name_portion(
    name: str,
    qty: str,
    unit: str,
) -> tuple[float, dict, Optional[str]]:
    """
    Fallback using the USDA weights index by ingredient name + unit, even when
    the canonical USDA link is unsuitable for the parsed unit.
    """
    qty_value = _parse_quantity_value(qty)
    if qty_value is None:
        raise ValueError("Invalid quantity value")

    matched = find_weight_match_by_name(name=name, unit=unit)
    if not matched:
        raise ValueError("No name-based portion match")

    portion = matched.get("portion") or {}
    grams_per_unit = portion.get("grams_per_unit")
    if grams_per_unit is None:
        grams = portion.get("grams")
        amount = portion.get("amount")
        try:
            if grams is not None and amount not in (None, 0):
                grams_per_unit = float(grams) / float(amount)
        except (TypeError, ValueError, ZeroDivisionError):
            grams_per_unit = None

    if grams_per_unit is None:
        raise ValueError("No grams_per_unit in name-based portion match")

    grams_per_unit = float(grams_per_unit)
    grams = float(qty_value) * grams_per_unit
    portion_match = {
        "portion_desc": portion.get("portion_desc"),
        "grams_per_unit": grams_per_unit,
        "source_food_name": matched.get("food_name"),
        "source": "weight_name_fallback",
    }
    usda_id = matched.get("usda_id")
    return grams, portion_match, (str(usda_id).strip() if usda_id else None)

def _food_group_for_link(link: Optional[dict], usda_id: Optional[str]) -> Optional[str]:
    if isinstance(link, dict):
        group = str(link.get("food_group") or "").strip()
        if group:
            return group
    if usda_id:
        linked = usda_id_to_link(str(usda_id))
        if isinstance(linked, dict):
            group = str(linked.get("food_group") or "").strip()
            if group:
                return group
    return None

def _has_herb_spice_hint(*texts: Optional[str]) -> bool:
    hay = " ".join(str(t or "").strip().lower() for t in texts if t)
    if not hay:
        return False
    return any(token in hay for token in HERB_SPICE_HINT_TOKENS)


def _name_tokens(name: Optional[str]) -> set[str]:
    return set(re.findall(r"[a-z]+", str(name or "").strip().lower()))


def _has_phrase(text: str, phrases: set[str]) -> bool:
    return any(" " in phrase and phrase in text for phrase in phrases)


def _has_token(tokens: set[str], token_set: set[str]) -> bool:
    return bool(tokens & {token for token in token_set if " " not in token})


def _coarse_food_class(
    text: Optional[str],
    food_group: Optional[str] = None,
) -> str:
    text_norm = re.sub(r"[^a-z0-9\s-]", " ", str(text or "").lower())
    text_norm = re.sub(r"\s+", " ", text_norm).strip()
    tokens = _name_tokens(text_norm)
    group_norm = str(food_group or "").strip().lower()

    if not text_norm and not group_norm:
        return "unknown"

    if _has_token(tokens, ALCOHOL_TOKENS):
        return "alcohol"
    if _has_token(tokens, STOCK_SAUCE_TOKENS):
        return "stock_sauce"
    if _has_token(tokens, GELATIN_THICKENER_TOKENS) or _has_phrase(text_norm, GELATIN_THICKENER_TOKENS):
        return "gelatin_thickener"
    if _has_phrase(text_norm, BAKING_POWDER_TOKENS):
        return "leavening"
    if _has_phrase(text_norm, BAKING_MIX_TOKENS):
        return "baking_mix"
    if _has_token(tokens, RICE_GRAIN_TOKENS):
        return "grain"
    if _has_token(tokens, PASTA_NOODLE_TOKENS):
        return "grain"
    if _has_token(tokens, EGG_TOKENS):
        return "egg"
    if _has_token(tokens, CHEESE_TOKENS):
        return "dairy_cheese"
    if _has_token(tokens, MILK_CREAM_TOKENS) or _has_phrase(text_norm, MILK_CREAM_TOKENS):
        return "dairy_milk_cream"
    if _has_token(tokens, BREAD_BAKED_TOKENS):
        return "baked_good"
    if _has_token(tokens, BEEF_TOKENS):
        return "meat_beef"
    if _has_token(tokens, LAMB_GOAT_TOKENS):
        return "meat_lamb_goat"
    if _has_token(tokens, PORK_TOKENS):
        return "meat_pork"
    if _has_token(tokens, POULTRY_TOKENS):
        return "meat_poultry"
    if _has_token(tokens, FISH_SHELLFISH_TOKENS):
        return "fish_shellfish"
    if _has_token(tokens, LEGUME_TOKENS):
        return "legume"
    if _has_token(tokens, VEGETABLE_TOKENS):
        return "vegetable"
    if _has_token(tokens, HERB_SPICE_CLASS_TOKENS):
        return "herb_spice"
    if _has_token(tokens, FRUIT_TOKENS):
        return "fruit"
    if _has_token(tokens, OIL_FAT_TOKENS):
        return "oil_fat"
    if _has_token(tokens, SWEETENER_TOKENS):
        return "sweetener"

    if "beef products" in group_norm:
        return "meat_beef"
    if "poultry products" in group_norm:
        return "meat_poultry"
    if "sausages and luncheon meats" in group_norm:
        return "meat_processed"
    if "finfish and shellfish" in group_norm:
        return "fish_shellfish"
    if "dairy and egg" in group_norm:
        return "dairy_egg_unknown"
    if "cereal grains and pasta" in group_norm:
        return "grain"
    if "legumes" in group_norm:
        return "legume"
    if "fruits and fruit juices" in group_norm:
        return "fruit"
    if "vegetables and vegetable products" in group_norm:
        return "vegetable"
    if "spices and herbs" in group_norm:
        return "herb_spice"
    if "fats and oils" in group_norm:
        return "oil_fat"
    if "sweets" in group_norm:
        return "sweet"
    if "baked products" in group_norm:
        return "baked_good"
    if "beverages" in group_norm:
        return "beverage"
    if "soups, sauces, and gravies" in group_norm:
        return "stock_sauce"

    return "unknown"


def _classes_compatible(source_class: str, candidate_class: str) -> bool:
    if "unknown" in {source_class, candidate_class}:
        return True
    if source_class == candidate_class:
        return True
    compatible_groups = [
        {"dairy_cheese", "dairy_milk_cream", "dairy_egg_unknown"},
        {"meat_beef", "meat_lamb_goat", "meat_pork", "meat_poultry", "meat_processed"},
        {"grain", "baked_good"},
        {"sweet", "sweetener"},
    ]
    return any(source_class in group and candidate_class in group for group in compatible_groups)


def _protein_identity_mismatch_reason(
    source_text: str,
    candidate_text: Optional[str],
) -> Optional[str]:
    source_identities = _name_tokens(source_text) & PROTEIN_IDENTITY_TOKENS
    candidate_identities = _name_tokens(candidate_text) & PROTEIN_IDENTITY_TOKENS
    if not source_identities or not candidate_identities:
        return None
    if source_identities & candidate_identities:
        return None
    source_identity = sorted(source_identities)[0]
    candidate_identity = sorted(candidate_identities)[0]
    return f"protein_identity_{source_identity}_vs_{candidate_identity}"


def _food_class_mismatch_reason(
    source_text: str,
    candidate_text: Optional[str],
    candidate_food_group: Optional[str] = None,
) -> Optional[str]:
    source_norm = re.sub(r"[^a-z0-9\s-]", " ", str(source_text or "").lower())
    candidate_norm = re.sub(r"[^a-z0-9\s-]", " ", str(candidate_text or "").lower())
    source_tokens = _name_tokens(source_norm)
    candidate_tokens = _name_tokens(candidate_norm)

    if source_tokens & ICE_TOKENS and (
        "frozen novelties" in candidate_norm
        or "ice type" in candidate_norm
        or "frozen desserts" in str(candidate_food_group or "").strip().lower()
    ):
        return "ice_vs_frozen_novelty"
    if source_tokens & EGG_TOKENS and candidate_tokens & BREAD_BAKED_TOKENS:
        return "egg_vs_baked_good"
    if source_tokens & FISH_SHELLFISH_TOKENS and (
        candidate_tokens & HERB_SPICE_CLASS_TOKENS
        or str(candidate_food_group or "").strip().lower() == HERB_SPICE_FOOD_GROUP.lower()
    ):
        return "fish_shellfish_vs_herb_spice"

    source_class = _coarse_food_class(source_text)
    candidate_class = _coarse_food_class(candidate_text, food_group=candidate_food_group)
    if not _classes_compatible(source_class, candidate_class):
        return f"{source_class}_vs_{candidate_class}"
    head_token_mismatch = _head_token_mismatch_reason(source_text, candidate_text)
    if head_token_mismatch is not None:
        return head_token_mismatch
    return _protein_identity_mismatch_reason(source_text, candidate_text)


def _usda_link_mismatch_reason(name: str, link: Optional[dict]) -> Optional[str]:
    if not isinstance(link, dict):
        return None
    candidate_text = (
        link.get("usda_food_label")
        or link.get("canonical")
        or link.get("name")
    )
    return _food_class_mismatch_reason(
        source_text=name,
        candidate_text=str(candidate_text or ""),
        candidate_food_group=link.get("food_group"),
    )


def _is_pinch_fallback_excluded(name: str) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return False
    if tokens & PINCH_FALLBACK_EXCLUDED_TOKENS:
        return True
    return "green" in tokens and "onion" in tokens


def _is_pinch_default_candidate(name: str, food_group: Optional[str], herb_spice_hint: bool) -> bool:
    if _is_pinch_fallback_excluded(name):
        return False
    name_tokens = _name_tokens(name)
    if "fresh" in name_tokens and name_tokens & _FRESH_HERB_TOKENS:
        return False
    if herb_spice_hint:
        return True
    group = str(food_group or "").strip().lower()
    if group == HERB_SPICE_FOOD_GROUP.lower():
        return True
    n = str(name or "").strip().lower()
    return any(tok in n for tok in ("salt", "pepper", "seasoning"))


def _normalize_lookup_ingredient(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _singularize_token(token: str) -> str:
    if token in {"aubergines", "courgettes"}:
        return token[:-1]
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("les", "ges")) and len(token) > 4:
        return token[:-1]
    if token.endswith("es") and not token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _lookup_name_variants(name: str) -> list[str]:
    base = _normalize_lookup_ingredient(name)
    if not base:
        return []
    variants = []

    def add_variant(value: str) -> None:
        value = _normalize_lookup_ingredient(value)
        if not value or value in variants:
            return
        variants.append(value)
        tokens = value.split()
        singular = " ".join(_singularize_token(t) for t in tokens).strip()
        if singular and singular != value and singular not in variants:
            variants.append(singular)

    add_variant(base)
    stripped = _strip_qualifiers(base)
    if stripped:
        add_variant(stripped)
    return variants


def _csv_rows_from_path_or_pg(path: Path, pg_name: str) -> list[dict]:
    """Return static rows from Postgres, then local CSV when DB is unavailable."""
    from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data

    try:
        return load_pipeline_data(pg_name)
    except Exception:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return []


@lru_cache(maxsize=1)
def _load_llm_unit_grams_index() -> dict:
    index_by_name: dict[str, dict[str, Any]] = {}
    index_by_usda_id: dict[str, dict[str, Any]] = {}
    sources = [(FDA_UNIT_GRAMS_CSV_PATH, "fda", "ingredient_unit_grams_fda")]
    if CACHED_LLM_UNIT_GRAMS_ENABLED:
        sources.append((LLM_UNIT_GRAMS_CSV_PATH, "llm", "ingredient_unit_grams_llm"))
    for path, source_tag, pg_name in sources:
        rows = _csv_rows_from_path_or_pg(path, pg_name)
        if not rows:
            continue
        for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue
            grams_raw = str(row.get("grams") or "").strip()
            if not grams_raw or grams_raw.upper() == "NA":
                continue
            try:
                grams = float(grams_raw)
            except ValueError:
                continue
            unit = str(row.get("unit") or row.get("reference_unit") or "").strip()
            payload = {
                "ingredient": ingredient,
                "unit": unit,
                "grams_per_unit": grams,
                "source": source_tag,
                "source_file": str(path),
                "usda_id": str(row.get("usda_id") or "").strip() or None,
            }
            for key in _lookup_name_variants(ingredient):
                index_by_name.setdefault(key, payload)
            usda_id = str(payload.get("usda_id") or "").strip()
            if usda_id:
                # Prefer earlier sources in `sources` order (FDA before LLM).
                index_by_usda_id.setdefault(usda_id, payload)
    return {"by_name": index_by_name, "by_usda_id": index_by_usda_id}


def _lookup_llm_unit_grams(name: str, usda_id: Optional[str] = None) -> Optional[dict]:
    if CACHED_LLM_UNIT_GRAMS_ENABLED:
        for key in _lookup_name_variants(name):
            found = _LIVE_LLM_WEIGHT_CACHE.get(key)
            if found:
                return {**found, "lookup_basis": "name"}

    index = _load_llm_unit_grams_index()
    by_name = index.get("by_name", {})
    for key in _lookup_name_variants(name):
        found = by_name.get(key)
        if found:
            return {**found, "lookup_basis": "name"}
    usda_id_norm = str(usda_id or "").strip()
    if usda_id_norm:
        by_usda_id = index.get("by_usda_id", {})
        found = by_usda_id.get(usda_id_norm)
        if found:
            return {**found, "lookup_basis": "usda_id"}
    return None


# Live-learned live-LLM resolutions, keyed by normalized ingredient name.
# Checked before both the persisted CSV/Postgres index and any new live-LLM
# call, so the same ingredient+unit is never re-estimated twice in one run —
# "garlic" pays for one LLM call, not one per occurrence. Also appended to
# LLM_UNIT_GRAMS_CSV_PATH so a future run can pick it up permanently once
# that CSV is re-imported into pipeline_static_data (same convention every
# other static dataset in this pipeline already follows).
_LIVE_LLM_WEIGHT_CACHE: dict[str, dict[str, Any]] = {}
_live_llm_weight_csv_header_written = LLM_UNIT_GRAMS_CSV_PATH.exists()


def _record_live_llm_resolution(
    name: str, unit: Optional[str], grams_per_unit: float, usda_id: Optional[str] = None
) -> None:
    global _live_llm_weight_csv_header_written
    if grams_per_unit <= 0:
        return
    payload = {
        "ingredient": name,
        "unit": unit or "",
        "grams_per_unit": grams_per_unit,
        "source": "live_llm",
        "source_file": str(LLM_UNIT_GRAMS_CSV_PATH),
        "usda_id": usda_id,
    }
    for key in _lookup_name_variants(name):
        _LIVE_LLM_WEIGHT_CACHE.setdefault(key, payload)

    try:
        LLM_UNIT_GRAMS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_header = not _live_llm_weight_csv_header_written
        with LLM_UNIT_GRAMS_CSV_PATH.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if write_header:
                # Match the existing file's 3-column shape exactly — it has
                # no usda_id column, and mixing row widths under one header
                # would misalign every DictReader consumer of this file.
                writer.writerow(["ingredient", "unit", "grams"])
                _live_llm_weight_csv_header_written = True
            writer.writerow([name, unit or "", grams_per_unit])
    except OSError:
        pass


@lru_cache(maxsize=1)
def _load_offline_reference_dataset_index() -> dict:
    """Load the verified offline rebuild dataset (one row per ingredient+unit).

    Returns a dict keyed by (ingredient_casefold, normalized_unit_casefold)
    mapping to {grams_per_unit, source_type, confidence, reference_qty}.

    No-op if the CSV is missing — the rebuild may not have been run yet.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    if not OFFLINE_REFERENCE_DATASET_ENABLED:
        return index
    try:
        rows = _csv_rows_from_path_or_pg(
            OFFLINE_REFERENCE_DATASET_CSV_PATH,
            "ingredient_unit_reference_dataset",
        )
    except (FileNotFoundError, KeyError):
        # Rebuild has not landed yet — silently no-op.
        return index
    if not rows:
        rows = []
    try:
        with REVIEWED_OPENROUTER_REFERENCE_CSV_PATH.open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            # Reviewed rows take precedence over the older bulk rebuild.
            rows = list(csv.DictReader(handle)) + rows
    except (FileNotFoundError, OSError, csv.Error):
        pass
    if not rows:
        return index

    for row in rows:
        ingredient = str(row.get("ingredient") or "").strip()
        unit_norm = str(row.get("normalized_unit") or "").strip()
        weight_raw = str(row.get("weight_grams") or "").strip()
        source_type = str(row.get("source_type") or "").strip().lower()
        if not ingredient or not unit_norm or not weight_raw:
            continue
        if source_type not in {"accepted_deterministic", "llm_rebuilt"}:
            continue
        if source_type == "llm_rebuilt" and not OFFLINE_LLM_REBUILT_ENABLED:
            continue
        try:
            weight = float(weight_raw)
        except ValueError:
            continue
        if weight <= 0:
            continue
        try:
            confidence = float(row.get("confidence") or 0.0)
        except ValueError:
            confidence = 0.0
        if source_type == "llm_rebuilt" and confidence < OFFLINE_REFERENCE_MIN_CONFIDENCE:
            continue
        ref_measurement = str(row.get("reference_measurement") or "").strip()
        ref_qty_value = _parse_quantity_value(
            (_split_measurement(ref_measurement) or (None, None, False))[0]
        ) or 1.0
        if ref_qty_value <= 0:
            ref_qty_value = 1.0
        grams_per_unit = weight / ref_qty_value
        payload = {
            "ingredient": ingredient,
            "normalized_unit": unit_norm,
            "grams_per_unit": grams_per_unit,
            "source_type": source_type,
            "confidence": confidence,
            "reference_measurement": ref_measurement,
        }
        for key in _lookup_name_variants(ingredient):
            index.setdefault((key, unit_norm.casefold()), payload)
    return index


def _lookup_offline_reference(
    name: str, unit: Optional[str]
) -> Optional[dict]:
    if not OFFLINE_REFERENCE_DATASET_ENABLED:
        return None
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None
    index = _load_offline_reference_dataset_index()
    if not index:
        return None
    unit_key = unit_norm.casefold()
    for key in _lookup_name_variants(name):
        hit = index.get((key, unit_key))
        if hit:
            return hit
    return None


def _measurement_signature(
    measurement: Any,
    parsed_qty: Optional[str] = None,
    parsed_unit: Optional[str] = None,
) -> Optional[tuple[float, str]]:
    qty = parsed_qty
    unit = parsed_unit
    if qty is None and unit is None:
        qty, unit, _ = _split_measurement(measurement)
    qty_value = _parse_quantity_value(qty)
    if qty_value is None:
        return None
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None
    return (round(float(qty_value), 6), str(unit_norm))


_PACKAGE_SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*"
    r"(?:oz|ounce|ounces|g|gram|grams|kg|kilogram|kilograms|lb|lbs|pound|pounds|"
    r"ml|milliliter|milliliters|l|liter|liters|fl\s*oz|fluid\s+ounce|fluid\s+ounces)\b",
    re.IGNORECASE,
)


def _has_explicit_package_size(*texts: Any) -> bool:
    return any(_PACKAGE_SIZE_RE.search(str(text or "")) for text in texts if text is not None)


def _explicit_package_size_grams(*texts: Any) -> Optional[float]:
    for text in texts:
        text_norm = str(text or "").strip().lower()
        if not text_norm:
            continue
        match = _PACKAGE_SIZE_RE.search(text_norm)
        if not match:
            continue
        size_text = re.sub(r"\s+", " ", match.group(0).replace("-", " ")).strip()
        size_match = re.match(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>.+)", size_text)
        if not size_match:
            continue
        try:
            value = float(size_match.group("value"))
        except ValueError:
            continue
        unit = re.sub(r"\s+", " ", size_match.group("unit").strip())
        unit = {
            "fl oz": "fluid ounce",
            "fluid ounces": "fluid ounce",
            "ounces": "ounce",
            "grams": "gram",
            "kilograms": "kilogram",
            "pounds": "pound",
            "milliliters": "milliliter",
            "liters": "liter",
        }.get(unit, unit)
        if unit in {"oz", "ounce"}:
            return value * _MASS_UNITS["oz"]
        if unit in {"g", "gram"}:
            return value
        if unit in {"kg", "kilogram"}:
            return value * _MASS_UNITS["kg"]
        if unit in {"lb", "lbs", "pound"}:
            return value * _MASS_UNITS["lb"]
        if unit in {"ml", "milliliter"}:
            return value
        if unit in {"l", "liter"}:
            return value * 1000.0
    return None


def _is_generic_cached_llm_package_portion(
    unit: Optional[str],
    grams_per_unit: float,
    *texts: Any,
) -> bool:
    unit_norm = _clean_unit(str(unit or ""))
    if unit_norm not in GENERIC_LLM_PACKAGE_UNITS:
        return False
    if round(float(grams_per_unit), 3) not in GENERIC_LLM_PACKAGE_WEIGHTS:
        return False
    return not _has_explicit_package_size(*texts)


@lru_cache(maxsize=1)
def _load_recipe1m_llm_weight_index() -> dict:
    index: dict[str, dict[str, Any]] = {}
    rows = _csv_rows_from_path_or_pg(RECIPE1M_LLM_WEIGHT_CSV_PATH, "recipe1m_unmatched_ingredient_weights_llm")
    if not rows:
        return index

    for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue

            llm_error = str(row.get("llm_error") or "").strip()
            grams_raw = str(row.get("llm_weight_grams") or "").strip()
            if llm_error or not grams_raw:
                continue
            try:
                grams = float(grams_raw)
            except ValueError:
                continue
            if grams <= 0:
                continue

            sample_measurement = str(row.get("sample_measurement") or "").strip()
            row_qty = str(row.get("parsed_quantity") or "").strip() or None
            row_unit = str(row.get("parsed_unit") or "").strip() or None
            parsed_signature = _measurement_signature(
                measurement=None,
                parsed_qty=row_qty,
                parsed_unit=row_unit,
            )
            sample_signature = _measurement_signature(sample_measurement)

            payload = {
                "ingredient": ingredient,
                "sample_measurement": sample_measurement,
                "llm_weight_grams": grams,
                "parsed_signature": parsed_signature,
                "sample_signature": sample_signature,
            }
            for key in _lookup_name_variants(ingredient):
                index.setdefault(key, payload)
    return index


def _lookup_recipe1m_llm_weight_fallback(
    name: str,
    measurement: Any,
    qty: Optional[str],
    unit: Optional[str],
) -> Optional[dict]:
    if not RECIPE1M_LLM_FALLBACK_ENABLED:
        return None
    index = _load_recipe1m_llm_weight_index()
    if not index:
        return None

    current_signature = _measurement_signature(
        measurement,
        parsed_qty=qty,
        parsed_unit=unit,
    )
    if current_signature is None:
        return None

    for key in _lookup_name_variants(name):
        row = index.get(key)
        if not row:
            continue
        if (
            current_signature == row.get("parsed_signature")
            or current_signature == row.get("sample_signature")
        ):
            return {
                "ingredient": row.get("ingredient"),
                "sample_measurement": row.get("sample_measurement"),
                "grams": float(row.get("llm_weight_grams")),
                "signature": current_signature,
            }
    return None


@lru_cache(maxsize=1)
def _load_recipe1m_llm_portion_fallback_index() -> dict:
    by_name_unit: dict[tuple[str, str], dict[str, Any]] = {}
    by_usda_unit: dict[tuple[str, str], dict[str, Any]] = {}
    rows = _csv_rows_from_path_or_pg(RECIPE1M_LLM_PORTION_FALLBACK_CSV_PATH, "food_weights_updated")
    if not rows:
        return {"by_name_unit": by_name_unit, "by_usda_unit": by_usda_unit}

    for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue
            grams_raw = str(row.get("weight_per_unit_grams") or "").strip()
            if not grams_raw:
                continue
            try:
                grams_per_unit = float(grams_raw)
            except ValueError:
                continue
            if grams_per_unit <= 0:
                continue

            unit_raw = str(row.get("extracted_unit") or "").strip()
            if not unit_raw:
                _, unit_raw, _ = _split_measurement(row.get("measurement"))
            unit_norm = _clean_unit(unit_raw)
            if not unit_norm:
                continue

            payload = {
                "ingredient": ingredient,
                "unit": unit_norm,
                "grams_per_unit": grams_per_unit,
                "sample_measurement": str(row.get("measurement") or "").strip(),
                "usda_id": str(row.get("usda_id") or "").strip() or None,
            }
            for key in _lookup_name_variants(ingredient):
                by_name_unit.setdefault((key, unit_norm), payload)
            usda_id = str(payload.get("usda_id") or "").strip()
            if usda_id:
                by_usda_unit.setdefault((usda_id, unit_norm), payload)

    return {"by_name_unit": by_name_unit, "by_usda_unit": by_usda_unit}


def _lookup_recipe1m_llm_portion_fallback(
    name: str,
    unit: Optional[str],
    usda_id: Optional[str] = None,
    measurement: Any = None,
) -> Optional[dict]:
    if not RECIPE1M_LLM_FALLBACK_ENABLED:
        return None
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None

    index = _load_recipe1m_llm_portion_fallback_index()
    usda_id_norm = str(usda_id or "").strip()
    if usda_id_norm:
        hit = index.get("by_usda_unit", {}).get((usda_id_norm, unit_norm))
        if hit:
            if _is_generic_cached_llm_package_portion(
                unit_norm,
                float(hit.get("grams_per_unit") or 0),
                name,
                measurement,
                hit.get("sample_measurement"),
            ):
                return None
            return hit

    for key in _lookup_name_variants(name):
        hit = index.get("by_name_unit", {}).get((key, unit_norm))
        if hit:
            if _is_generic_cached_llm_package_portion(
                unit_norm,
                float(hit.get("grams_per_unit") or 0),
                name,
                measurement,
                hit.get("sample_measurement"),
            ):
                return None
            return hit
    return None


def _llm_portion_plausibility_error(
    name: str,
    unit: Optional[str],
    grams_per_unit: float,
    food_group: Optional[str] = None,
) -> Optional[str]:
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return "missing_unit"
    if grams_per_unit <= 0:
        return "non_positive_weight"

    tokens = _name_tokens(name)
    food_group_norm = str(food_group or "").strip().lower()

    if unit_norm == "whole" and tokens & WHOLE_ANIMAL_TOKENS and grams_per_unit < 500:
        return "whole_animal_too_small"
    if unit_norm == "whole" and "products" in food_group_norm and grams_per_unit < 25:
        return "whole_food_too_small"
    if unit_norm in {"box", "package"} and grams_per_unit < 20:
        return "package_too_small"
    if unit_norm in {"bag", "bags"} and grams_per_unit < 20:
        return "bag_too_small"
    if unit_norm == "bunch" and grams_per_unit < 5:
        return "bunch_too_small"
    if unit_norm in {"cup", "tablespoon", "teaspoon"} and grams_per_unit > 500:
        return "volume_unit_too_large"
    _tiny_unit_max = {
        "clove": 25.0,
        "sprig": 15.0,
        "leaf": 5.0,
        "leaves": 5.0,
        "pinch": 5.0,
        "dash": 5.0,
        "teaspoon": 25.0,
        "tablespoon": 60.0,
    }.get(unit_norm)
    if _tiny_unit_max is not None and grams_per_unit > _tiny_unit_max:
        return f"{unit_norm}_too_heavy"
    if grams_per_unit > LLM_IMPLAUSIBLE_PER_UNIT_WEIGHT_GRAMS:
        return "implausible_per_unit_weight"
    # This guard exists to catch the LLM echoing a generic unit-conversion
    # constant (1 lb -> 453.59g, 1 L -> 1000g) instead of actually reasoning
    # about the food. "piece"/"whole"/"each" have no such conversion to
    # confuse with, so a real per-item weight that happens to land near one
    # of those constants (a ~1kg melon, a ~1lb loaf) is coincidence, not a
    # hallucinated default — checking it here only produced false positives.
    if unit_norm not in {"piece", "pieces", "whole", "each", "item", "items"}:
        for constant, ok_units in LLM_SUSPICIOUS_DEFAULT_WEIGHTS.items():
            if (
                abs(grams_per_unit - constant) <= max(0.5, constant * 0.002)
                and unit_norm not in ok_units
            ):
                return f"suspicious_unit_conversion_default:{constant}"
    return None


def _llm_weight_plausibility_error(
    name: str,
    qty: Optional[str],
    unit: Optional[str],
    grams: float,
    food_group: Optional[str] = None,
) -> Optional[str]:
    qty_value = _parse_quantity_value(qty)
    if qty_value is None or qty_value <= 0:
        qty_value = 1.0
    return _llm_portion_plausibility_error(
        name=name,
        unit=unit,
        grams_per_unit=float(grams) / float(qty_value),
        food_group=food_group,
    )


_POURABLE_CONDIMENT_TOKENS = {
    "syrup", "oil", "sauce", "honey", "juice", "milk", "cream", "vinegar",
    "dressing", "gravy", "stock", "broth", "puree", "paste", "jam", "jelly",
    "yogurt", "yoghurt", "custard", "water", "extract", "essence",
    "sugar", "flour", "rice", "wine", "linseed", "linseeds",
}


def _default_unit_for_unknown_ingredient(name: str) -> str:
    """Last-resort unit when neither the measurement nor the name gives one.

    "piece" is a reasonable default for a countable/solid item ("1 piece of
    chicken") but nonsensical for a pourable ("1 piece of maple syrup") —
    asking the LLM to weigh "1 piece" of a liquid reliably makes it return
    0 rather than guess. Route those to "tablespoon" instead.
    """
    tokens = set(re.split(r"[\s,-]+", str(name).strip().lower()))
    if tokens & _POURABLE_CONDIMENT_TOKENS:
        return "tablespoon"
    return "piece"


def _live_llm_weight_fallback(
    name: str,
    qty: Optional[str],
    unit: Optional[str],
) -> tuple[Optional[float], Optional[str], str]:
    """Returns (grams, error, unit_used).

    ``unit_used`` is the unit actually sent to the LLM — it's ``unit`` when
    given, otherwise a guessed one. Callers MUST use it (not the original,
    possibly-``None`` ``unit``) for a follow-up plausibility check: checking
    against the original unit made the plausibility gate see "missing_unit"
    on every bare-count ingredient this fallback exists to rescue, silently
    discarding a good estimate and falling through to a 0 g weight.
    """
    if not LIVE_WEIGHT_LLM_ENABLED:
        return None, "live_llm_disabled", unit or _default_unit_for_unknown_ingredient(name)

    qty_for_llm = qty
    if qty_for_llm is None or not str(qty_for_llm).strip():
        qty_for_llm = "1"

    unit_for_llm = unit
    if unit_for_llm is None or not str(unit_for_llm).strip():
        unit_for_llm = (
            _infer_unit_from_name(name)
            or _default_unit_for_unknown_ingredient(name)
        )
    try:
        grams = ingredient_weight_llm_tool.invoke(
            {
                "ingredient": name,
                "parsed_quantity": qty_for_llm,
                "parsed_unit": unit_for_llm,
            }
        )
    except Exception as exc:
        return None, f"live_llm_error:{exc}", unit_for_llm

    try:
        grams_f = float(grams)
    except (TypeError, ValueError):
        return None, f"live_llm_non_numeric:{grams!r}", unit_for_llm
    if grams_f < 0:
        return None, f"live_llm_negative:{grams_f}", unit_for_llm
    return grams_f, None, unit_for_llm


def _weight_name_usda_link(name: str, unit: Optional[str]) -> Optional[dict]:
    if not unit:
        return None
    try:
        matched = find_weight_match_by_name(name=name, unit=unit)
    except Exception:
        return None
    if not matched:
        return None
    usda_id = str(matched.get("usda_id") or "").strip()
    if not usda_id:
        return None
    food_name = str(matched.get("food_name") or "").strip() or None
    return {
        "usda_id": usda_id,
        "canonical_id": None,
        "canonical": food_name,
        "usda_food_label": food_name,
        "food_group_id": None,
        "food_group": None,
        "distance": None,
        "similarity": None,
        "match_source": "weight_name_fallback",
        "match_collection": "usda_weights_json",
    }


def _detail_source(detail: dict) -> str:
    if bool(detail.get("fda_fallback")):
        return "FDA"
    if bool(detail.get("live_llm_fallback")) or bool(detail.get("llm_fallback")):
        return "LLM fallback"
    match_type = str(detail.get("match_type") or "").lower()
    if "live_llm" in match_type or "llm" in match_type:
        return "LLM fallback"
    if "fda" in match_type:
        return "FDA"
    return "USDA portion tables"


def _detail_match(detail: dict) -> Optional[str]:
    canonical = str(detail.get("usda_match_canonical") or "").strip()
    if canonical:
        return canonical
    usda_id = str(detail.get("usda_id") or "").strip()
    if usda_id:
        return usda_id
    portion = detail.get("portion_match") or {}
    if isinstance(portion, dict):
        source_food = str(portion.get("source_food_name") or "").strip()
        if source_food:
            return source_food
    return None


def _compute_confidence(detail: dict) -> tuple[float, str]:
    match_type = str(detail.get("match_type") or "").strip()
    match_type_norm = match_type.lower()
    source = str(detail.get("usda_match_source") or "").strip().lower()
    similarity = detail.get("usda_match_similarity")
    error = detail.get("error")

    if error:
        base = 0.0
        reason = str(error)
    elif match_type_norm in {"direct_mass", "direct_mass_edible_yield"}:
        base = 1.0
        reason = "direct mass conversion"
    elif match_type_norm == "blank_quantity_default":
        base = 0.30
        reason = "policy default for a line with no amount"
    elif match_type_norm == "to_taste_min":
        base = 0.9
        reason = "deliberate minimal weight for optional/to-taste measurement"
    elif match_type_norm == "non_food_ingredient_minimal":
        base = 0.9
        reason = "deliberate minimal weight for non-food equipment/supply"
    elif match_type_norm == OFFLINE_REFERENCE_MATCH_TYPE:
        # Vetted by the offline rebuild pipeline. Source distinguishes
        # accepted-deterministic (highest trust) from llm_rebuilt (LLM
        # corrections that passed the verifier).
        source_type = str(detail.get("offline_reference_source_type") or "").lower()
        try:
            ref_conf = float(detail.get("offline_reference_confidence") or 0.0)
        except (TypeError, ValueError):
            ref_conf = 0.0
        if source_type == "accepted_deterministic":
            base = 0.90
            reason = "offline reference (accepted deterministic)"
        else:
            base = max(0.70, min(0.88, ref_conf))
            reason = "offline reference (LLM-rebuilt, verifier-passed)"
    elif match_type_norm == "direct" and source == "direct":
        base = 0.92
        reason = "direct USDA portion"
    elif match_type_norm in {"direct", "density_fallback"} and (
        source.startswith("embedding") or source.startswith("hybrid")
    ):
        try:
            base = max(0.45, float(similarity))
        except (TypeError, ValueError):
            base = 0.45
        reason = "hybrid USDA portion" if source.startswith("hybrid") else "embedding USDA portion"
    elif match_type_norm == "density_fallback":
        base = 0.65
        reason = "USDA density fallback"
    elif match_type_norm in {
        "common_unit_reference_fallback",
        "size_adjusted_reference_unit_fallback",
        "water_like_volume_fallback",
        "liquid_density_volume_fallback",
        "explicit_package_size_fallback",
    }:
        portion_desc = str((detail.get("portion_match") or {}).get("portion_desc") or "").lower()
        generic_count = any(
            marker in portion_desc
            for marker in ("fish fillet", "meat chop", "chicken breast", "large prawn", "sausage link")
        )
        base = 0.45 if generic_count else 0.65
        reason = "generic count reference" if generic_count else match_type
    elif match_type_norm == "reviewed_cup_density_fallback":
        base = 0.65
        reason = "reviewed USDA cup density"
    elif match_type_norm == "reviewed_item_weight_fallback":
        base = 0.50
        reason = "reviewed item/container weight (USDA or convention)"
    elif match_type_norm == "generic_spoon_convention":
        base = 0.35
        reason = "generic spoon convention for a small amount"
    elif match_type_norm == "weight_name_portion_fallback":
        base = 0.60
        reason = "USDA name portion fallback"
    elif match_type_norm == FDA_UNIT_GRAMS_MATCH_TYPE:
        base = 0.58
        reason = "FDA reference fallback"
    elif match_type_norm in {
        LLM_UNIT_GRAMS_MATCH_TYPE,
        RECIPE1M_LLM_PORTION_FALLBACK_MATCH_TYPE,
    }:
        base = 0.45
        reason = "cached LLM reference fallback"
    elif match_type_norm in {
        "pinch_default_missing_quantity_fallback",
        "herb_pinch_fallback",
    }:
        base = 0.35
        reason = "pinch/herb fallback"
    elif match_type_norm == RECIPE1M_LLM_WEIGHT_MATCH_TYPE:
        base = 0.40
        reason = "cached Recipe1M LLM fallback"
    elif match_type_norm.startswith("live_llm_"):
        base = 0.70
        reason = "live LLM fallback"
    elif detail.get("weight_grams") is not None:
        base = 0.50
        reason = "unclassified successful fallback"
    else:
        base = 0.0
        reason = "missing weight"

    penalty = 0.0
    if detail.get("unit_inferred"):
        penalty += 0.06
    if detail.get("quantity_inferred"):
        penalty += 0.06
    return max(0.0, min(1.0, base - penalty)), reason


def _annotate_confidence(detail: dict) -> dict:
    confidence, reason = _compute_confidence(detail)
    detail["confidence"] = confidence
    detail["confidence_reason"] = reason
    return detail


# Edible yield of a whole ready-to-cook bird (USDA SR portions "yield from 1 lb
# ready-to-cook": chicken 276 g meat+skin / 197 g meat only, duck 287 / 137 g,
# per 453.59 g). The matched nutrient row is per 100 g of edible meat, so a bone-in
# purchase weight must be scaled down. Triggered only by the word "whole" in the
# name (the snapshot layer adds it from display/note evidence); bird parts are exempt.
_WHOLE_BIRD_YIELD = {"chicken": (0.608, 0.434), "duck": (0.633, 0.302)}
_BIRD_PART_TOKENS = {
    "leg", "legs", "thigh", "thighs", "breast", "breasts", "wing", "wings",
    "drumstick", "drumsticks", "fillet", "fillets", "liver", "stock", "broth",
}


def _whole_bird_edible_factor(name: str) -> Optional[float]:
    tokens = set(re.findall(r"[a-z]+", str(name or "").lower()))
    if "whole" not in tokens or tokens & _BIRD_PART_TOKENS:
        return None
    for bird, (with_skin, meat_only) in _WHOLE_BIRD_YIELD.items():
        if bird in tokens:
            return meat_only if tokens & {"skinless", "skinned"} else with_skin
    return None


def _apply_whole_bird_edible_yield(names: list[str], details: list[dict], weights: list[float]) -> None:
    for index, (name, detail) in enumerate(zip(names, details)):
        factor = _whole_bird_edible_factor(name)
        purchased = weights[index]
        match_type = detail.get("match_type")
        if factor is None or purchased <= 0 or match_type not in {
            "direct_mass", "common_unit_reference_fallback",
        }:
            continue
        weights[index] = purchased * factor
        detail["weight_grams"] = weights[index]
        detail["purchased_grams"] = purchased
        detail["edible_yield_factor"] = factor
        if match_type == "direct_mass":
            detail["match_type"] = "direct_mass_edible_yield"


def _apply_blank_quantity_defaults(names: list[str], measures: list[Any], details: list[dict], weights: list[float]) -> None:
    for index, (name, detail) in enumerate(zip(names, details)):
        measurement = measures[index] if index < len(measures) else None
        is_oil = bool(re.search(r"\boil\b", str(name), re.IGNORECASE)) and "spray" not in str(name).lower()
        text = str(measurement or "").strip()
        # A bare number with no unit on an oil ("1 olive oil") is a spoon-sized amount: same oil default.
        bare_oil = is_oil and bool(re.fullmatch(r"\d+(?:\.\d+)?", text))
        allowed = {"missing_quantity", "missing_usda_id"} | ({"missing_unit"} if bare_oil else set())
        if (text and not bare_oil) or weights[index] > 0 or detail.get("error") not in allowed:
            continue
        grams = BLANK_OIL_GRAMS if is_oil else BLANK_DEFAULT_GRAMS
        weights[index] = grams
        detail.update({
            "weight_grams": grams,
            "error": None,
            "match_type": "blank_quantity_default",
            "blank_quantity_policy": "oil_default" if is_oil else "negligible_default",
            "portion_match": {"portion_desc": "blank quantity policy default", "grams_per_unit": grams},
        })
        _annotate_confidence(detail)


def _apply_low_confidence_live_llm(details: list[dict], weights: list[float]) -> None:
    for idx, detail in enumerate(details):
        _annotate_confidence(detail)
        if detail.get("error") or detail.get("live_llm_fallback"):
            continue
        try:
            confidence = float(detail.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence >= LIVE_LLM_CONFIDENCE_THRESHOLD:
            continue
        if detail.get("weight_grams") is None:
            continue

        live_llm_grams, live_llm_error, live_llm_unit = _live_llm_weight_fallback(
            name=str(detail.get("name") or ""),
            qty=detail.get("parsed_quantity"),
            unit=detail.get("parsed_unit"),
        )
        if live_llm_grams is None:
            detail["live_llm_fallback"] = False
            detail["live_llm_error"] = live_llm_error
            continue

        # Verify the LLM estimate the same way the terminal live-LLM path does.
        # On rejection keep the (low-confidence but plausible) deterministic
        # result rather than swapping in a bad LLM value. Checked against the
        # unit the LLM actually used (live_llm_unit), not the original
        # parsed_unit — that unit may be None, which always reads as
        # "missing_unit" and would reject every successful estimate.
        plausibility_error = _llm_weight_plausibility_error(
            name=str(detail.get("name") or ""),
            qty=detail.get("parsed_quantity"),
            unit=live_llm_unit,
            grams=float(live_llm_grams),
            food_group=detail.get("food_group"),
        )
        if plausibility_error is not None:
            detail["live_llm_fallback"] = False
            detail["live_llm_error"] = f"rejected_llm_estimate ({plausibility_error})"
            continue

        pre_llm_weight = detail.get("weight_grams")
        pre_llm_match_type = detail.get("match_type")
        pre_llm_confidence = detail.get("confidence")
        detail["pre_llm_weight_grams"] = pre_llm_weight
        detail["pre_llm_match_type"] = pre_llm_match_type
        detail["pre_llm_confidence"] = pre_llm_confidence
        detail["portion_match"] = {
            "portion_desc": "live LLM fallback",
            "grams_per_unit": None,
        }
        detail["match_type"] = "live_llm_low_confidence_fallback"
        detail["weight_grams"] = float(live_llm_grams)
        detail["error"] = None
        detail["live_llm_fallback"] = True
        detail["live_llm_reason"] = "low_confidence"
        detail["confidence"] = 0.70
        detail["confidence_reason"] = "live LLM fallback after low-confidence deterministic result"

        qty_value = _parse_quantity_value(detail.get("parsed_quantity")) or 1.0
        _record_live_llm_resolution(
            name=str(detail.get("name") or ""),
            unit=live_llm_unit,
            grams_per_unit=float(live_llm_grams) / qty_value,
            usda_id=detail.get("usda_id"),
        )
        if idx < len(weights):
            weights[idx] = float(live_llm_grams)


def _compact_detail(detail: dict) -> dict:
    return {
        "name": detail.get("name"),
        "parsed_quantity": detail.get("parsed_quantity"),
        "parsed_unit": detail.get("parsed_unit"),
        "quantity_inferred": detail.get("quantity_inferred"),
        "unit_inferred": detail.get("unit_inferred"),
        "match": _detail_match(detail),
        "match_type": detail.get("match_type"),
        "weight_grams": detail.get("weight_grams"),
        "source": _detail_source(detail),
        "error": detail.get("error"),
        "confidence": detail.get("confidence"),
        "confidence_reason": detail.get("confidence_reason"),
        "live_llm_fallback": detail.get("live_llm_fallback"),
        "live_llm_reason": detail.get("live_llm_reason"),
        "pre_llm_weight_grams": detail.get("pre_llm_weight_grams"),
        "pre_llm_match_type": detail.get("pre_llm_match_type"),
        "pre_llm_confidence": detail.get("pre_llm_confidence"),
        "offline_reference": detail.get("offline_reference"),
        "offline_reference_source_type": detail.get("offline_reference_source_type"),
        "offline_reference_confidence": detail.get("offline_reference_confidence"),
        "blank_quantity_policy": detail.get("blank_quantity_policy"),
        "purchased_grams": detail.get("purchased_grams"),
        "edible_yield_factor": detail.get("edible_yield_factor"),
    }


@tool
def ingredient_weight_tool_usda(
    ingredient_names: Any,
    measurements: Any,
    return_details: bool = False,
    debug: bool = False,
) -> list[float] | dict:
    """
    Calculates estimated weights using USDA portion data when possible.
    """
    names_list = [str(v) for v in _as_list(ingredient_names)]
    measures_list = _as_list(measurements)

    weights: list[float] = []
    details: list[dict] = []
    blank_salt_indexes: set[int] = set()
    for idx, name in enumerate(names_list):
        measurement = measures_list[idx] if idx < len(measures_list) else None
        if _is_blank_salt(name, measurement):
            measurement = "1 pinch"
            blank_salt_indexes.add(idx)
        qty, unit, qty_inferred = _split_measurement(measurement)
        # Some source lines keep the authoritative total mass in the ingredient
        # name (for example measurement="4 medium", name="(600g) chicken
        # thighs"). Always prefer that explicit mass over the outer count;
        # otherwise four 600 g total pieces become 2.4 kg.
        extracted = _extract_leading_measurement_from_name(name)
        if extracted is not None:
            qty, unit, name = extracted
            qty_inferred = False
        elif unit is None and not _is_large_bare_number_quantity(qty):
            # Bare unit abbreviation fused into the name ("c. flour" + "1/3").
            lead_unit = _extract_leading_unit_from_name(name)
            if lead_unit is not None:
                unit, name = lead_unit
                if qty is None:
                    qty = "1"
                    qty_inferred = True
        unit_missing_from_measurement = unit is None
        unit_inferred = False
        # Equipment/supplies (popsicle sticks, paper straws, ...) are not
        # food. Guessing a gram weight for one — whether via a USDA/embedding
        # mismatch or the live-LLM fallback — invents mass that dilutes the
        # recipe's real nutrient concentration. Short-circuit here, before
        # any resolution path runs, with the same negligible placeholder a
        # to-taste seasoning gets.
        if is_unambiguous_non_food_ingredient(name):
            weights.append(TO_TASTE_MIN_GRAMS)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": False,
                "usda_id": None,
                "food_group": None,
                "usda_match_source": None,
                "usda_match_similarity": None,
                "usda_match_collection": None,
                "usda_match_canonical": None,
                "portion_match": {
                    "portion_desc": "non-food ingredient minimal fallback",
                    "grams_per_unit": TO_TASTE_MIN_GRAMS,
                },
                "match_type": "non_food_ingredient_minimal",
                "weight_grams": TO_TASTE_MIN_GRAMS,
                "error": None,
                "fallback": True,
            })
            continue
        # Scrape artifacts, not ingredients at all: bare punctuation ("_",
        # "...", ":"), single-letter placeholders ("x"), or a markdown-bold
        # section heading captured as a line item ("**Other Necessary
        # Tools/Equipment**"). No amount of weight-estimation logic makes
        # these into a real ingredient; asking the LLM to weigh "_" invents
        # mass from nothing. Same minimal-placeholder treatment as non-food.
        if _is_garbage_ingredient_name(name):
            weights.append(TO_TASTE_MIN_GRAMS)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": False,
                "usda_id": None,
                "food_group": None,
                "usda_match_source": None,
                "usda_match_similarity": None,
                "usda_match_collection": None,
                "usda_match_canonical": None,
                "portion_match": {
                    "portion_desc": "garbage/heading name minimal fallback",
                    "grams_per_unit": TO_TASTE_MIN_GRAMS,
                },
                "match_type": "garbage_name_minimal",
                "weight_grams": TO_TASTE_MIN_GRAMS,
                "error": None,
                "fallback": True,
            })
            continue
        # A scraper that can't separate quantity from ingredient text often
        # duplicates the name into the measurement field verbatim ("Fresh
        # mint sprigs" / "Fresh mint sprigs") rather than leaving it blank —
        # there is no real quantity signal there, same as an explicit
        # to-taste/optional measurement. Normalize a trailing plural ("spoon"
        # vs "spoons") and a trailing "(as needed)"/"(optional)" note before
        # comparing, since those are the same near-duplicate, not a real
        # measurement.
        name_equals_measurement = bool(str(name or "").strip()) and (
            _normalize_for_duplicate_check(name) == _normalize_for_duplicate_check(measurement)
        )
        if (
            _is_zero_measurement(measurement)
            or name_equals_measurement
            or _is_blank_negligible_ingredient(name, measurement)
        ):
            weights.append(TO_TASTE_MIN_GRAMS)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": None,
                "parsed_unit": None,
                "quantity_inferred": False,
                "unit_inferred": False,
                "usda_id": None,
                "food_group": None,
                "usda_match_source": None,
                "usda_match_similarity": None,
                "usda_match_collection": None,
                "usda_match_canonical": None,
                "portion_match": {
                    "portion_desc": "optional/to-taste minimal fallback",
                    "grams_per_unit": TO_TASTE_MIN_GRAMS,
                },
                "match_type": "to_taste_min",
                "weight_grams": TO_TASTE_MIN_GRAMS,
                "error": None,
                "fallback": True,
            })
            continue
        if qty is not None and unit is None and _is_large_bare_number_quantity(qty):
            unit = "gram"
            unit_inferred = True
        elif qty is not None and unit is None:
            inferred_unit = _infer_unit_from_name(name)
            if inferred_unit:
                unit = inferred_unit
                unit_inferred = True
        elif qty is None:
            inferred_unit = _infer_unit_from_name(name, bare_count=False)
            # A plural scallion name is countable when a source quantity is
            # present ("2 scallions"), but blank "scallions" does not imply
            # one. Preserve the missing-quantity abstention.
            if inferred_unit and not (
                inferred_unit == "scallion"
                and str(name or "").strip().lower().endswith("s")
            ):
                qty = "1"
                unit = inferred_unit
                qty_inferred = True
                unit_inferred = True

        # Verified offline reference dataset: short-circuit when a vetted
        # (ingredient, normalized_unit) entry exists. Takes precedence over
        # the live USDA/embedding cascade because it represents auditable
        # frozen truth for the recurring signatures Neo4j actually contains.
        offline_ref = (
            _lookup_offline_reference(name, unit)
            if (qty is not None and unit is not None)
            else None
        )
        # A code-reviewed deterministic kitchen reference is stronger than an
        # LLM-rebuilt offline row. Keep accepted deterministic offline truth,
        # but do not let a generic 200 g LLM estimate override a known small
        # lamb cutlet or dried-chile portion.
        if (
            offline_ref is not None
            and offline_ref.get("source_type") == "llm_rebuilt"
            and _common_unit_reference_grams(name, unit, measurement=measurement)
            is not None
        ):
            offline_ref = None
        if offline_ref is not None:
            qty_value = _parse_quantity_value(qty)
            if qty_value is not None:
                grams_per_unit = float(offline_ref["grams_per_unit"])
                grams = qty_value * grams_per_unit
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": None,
                    "food_group": None,
                    "usda_match_source": None,
                    "usda_match_similarity": None,
                    "usda_match_collection": None,
                    "usda_match_canonical": None,
                    "portion_match": {
                        "portion_desc": (
                            f"offline reference ({offline_ref['source_type']})"
                        ),
                        "grams_per_unit": grams_per_unit,
                    },
                    "match_type": OFFLINE_REFERENCE_MATCH_TYPE,
                    "weight_grams": float(grams),
                    "error": None,
                    "fallback": False,
                    "offline_reference": True,
                    "offline_reference_source_type": offline_ref["source_type"],
                    "offline_reference_confidence": offline_ref["confidence"],
                    "offline_reference_note": OFFLINE_REFERENCE_NOTE,
                })
                continue

        link = canonical_name_to_usda(name)
        usda_match_source = "direct" if link else None
        usda_match_similarity = None
        rejected_usda_link = None
        rejected_usda_link_reason = None
        if link:
            rejected_usda_link_reason = _usda_link_mismatch_reason(name, link)
            if rejected_usda_link_reason is not None:
                rejected_usda_link = link
                link = None
                usda_match_source = None
        if not link:
            link = _embedding_usda_link(name, unit=unit)
            if link:
                usda_match_source = str(link.get("match_source") or "embedding")
                usda_match_similarity = link.get("hybrid_score") or link.get("similarity")
        if not link:
            link = _weight_name_usda_link(name, unit=unit)
            if link:
                usda_match_source = str(link.get("match_source") or "weight_name_fallback")
        usda_match_collection = None if not link else link.get("match_collection")
        usda_id = link.get("usda_id") if link else None
        food_group = _food_group_for_link(link, usda_id)
        herb_spice_hint = _has_herb_spice_hint(name)
        powder_hint = "powder" in str(name or "").strip().lower()
        error = None
        portion_match = None
        match_type = None
        llm_weight_fallback_attempted = False
        llm_weight_fallback_failed = False

        reference_fallback = _reference_weight_fallback(
            name,
            qty,
            unit,
            food_group=food_group,
            canonical=None if not link else link.get("canonical"),
            measurement=measurement,
        )
        if reference_fallback is not None:
            grams, portion_match, match_type = reference_fallback
            weights.append(float(grams))
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": unit_inferred,
                "usda_id": usda_id,
                "food_group": food_group,
                "usda_match_source": usda_match_source,
                "usda_match_similarity": usda_match_similarity,
                "usda_match_collection": usda_match_collection,
                "usda_match_canonical": None if not link else link.get("canonical"),
                "portion_match": portion_match,
                "match_type": match_type,
                "weight_grams": float(grams),
                "error": None,
                "fallback": True,
            })
            continue

        if qty is not None and unit is not None and str(unit).strip().lower() in _MASS_UNITS:
            unit_norm = str(unit).strip().lower()
            qty_value = _parse_quantity_value(qty)
            if qty_value is not None:
                grams = qty_value * _MASS_UNITS[unit_norm]
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": usda_id,
                    "food_group": food_group,
                    "usda_match_source": usda_match_source,
                    "usda_match_similarity": usda_match_similarity,
                    "usda_match_collection": usda_match_collection,
                    "usda_match_canonical": None if not link else link.get("canonical"),
                    "portion_match": {
                        "portion_desc": "direct mass conversion",
                        "grams_per_unit": _MASS_UNITS[unit_norm],
                    },
                    "match_type": "direct_mass",
                    "weight_grams": float(grams),
                    "error": None,
                    "fallback": False,
                })
                continue

        # Herbs/spices fallback: if quantity exists but unit is missing, treat it as pinch.
        if (
            usda_id
            and qty is not None
            and unit_missing_from_measurement
            and (
                (food_group == HERB_SPICE_FOOD_GROUP and herb_spice_hint)
                or powder_hint
            )
        ):
            qty_value = _parse_quantity_value(qty)
            if qty_value is not None:
                grams = qty_value * HERB_MISSING_UNIT_FALLBACK_GRAMS
                portion_match = {
                    "portion_desc": f"{HERB_MISSING_UNIT_FALLBACK_UNIT} (fallback)",
                    "grams_per_unit": HERB_MISSING_UNIT_FALLBACK_GRAMS,
                }
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": usda_id,
                    "food_group": food_group,
                    "usda_match_source": usda_match_source,
                    "usda_match_similarity": usda_match_similarity,
                    "usda_match_collection": usda_match_collection,
                    "usda_match_canonical": None if not link else link.get("canonical"),
                    "portion_match": portion_match,
                    "match_type": "herb_pinch_fallback",
                    "weight_grams": float(grams),
                    "error": None,
                })
                continue

        # Curated fallback (FDA/LLM): when quantity exists but unit is missing,
        # reuse ingredient-level grams-per-reference-unit.
        if usda_id and qty is not None and unit_missing_from_measurement:
            qty_value = _parse_quantity_value(qty)
            llm_fallback = _lookup_llm_unit_grams(name, usda_id=str(usda_id))
            # A USDA/FDA row keyed only by food ID may describe a different
            # portion from the noun in the source line. For example, eight
            # "cabbage leaves" previously inherited the FDA weight for eight
            # whole medium cabbage heads. An inferred leaf/slice/stalk unit
            # must have a name-specific reference, not a food-ID-only one.
            if (
                llm_fallback is not None
                and unit_inferred
                and llm_fallback.get("lookup_basis") == "usda_id"
            ):
                llm_fallback = None
            if qty_value is not None and llm_fallback is not None:
                grams_per_unit = float(llm_fallback["grams_per_unit"])
                grams = qty_value * grams_per_unit
                llm_unit = str(llm_fallback.get("unit") or "").strip()
                fallback_source = str(llm_fallback.get("source") or "llm").strip().lower()
                fallback_source_file = str(
                    llm_fallback.get("source_file")
                    or (
                        FDA_UNIT_GRAMS_CSV_PATH
                        if fallback_source == "fda"
                        else LLM_UNIT_GRAMS_CSV_PATH
                    )
                )
                if fallback_source == "fda":
                    fallback_match_type = FDA_UNIT_GRAMS_MATCH_TYPE
                    fallback_note = FDA_UNIT_GRAMS_NOTE
                    fallback_label = "FDA reference fallback"
                else:
                    fallback_match_type = LLM_UNIT_GRAMS_MATCH_TYPE
                    fallback_note = LLM_UNIT_GRAMS_NOTE
                    fallback_label = "LLM fallback"
                portion_desc = f"{llm_unit} ({fallback_label})" if llm_unit else f"{fallback_label} unit"
                portion_match = {
                    "portion_desc": portion_desc,
                    "grams_per_unit": grams_per_unit,
                }
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": usda_id,
                    "food_group": food_group,
                    "usda_match_source": usda_match_source,
                    "usda_match_similarity": usda_match_similarity,
                    "usda_match_collection": usda_match_collection,
                    "usda_match_canonical": None if not link else link.get("canonical"),
                    "portion_match": portion_match,
                    "match_type": fallback_match_type,
                    "weight_grams": float(grams),
                    "error": None,
                    "llm_fallback": fallback_source == "llm",
                    "fda_fallback": fallback_source == "fda",
                    "fallback": True,
                    "fallback_source": fallback_source,
                    "fallback_note": fallback_note,
                    "llm_fallback_note": fallback_note,
                    "llm_fallback_source_file": fallback_source_file,
                    "llm_fallback_ingredient": llm_fallback.get("ingredient"),
                    "llm_fallback_usda_id": llm_fallback.get("usda_id"),
                })
                continue

        # LLM fallback (Recipe1M unmatched set): when USDA match is still missing,
        # reuse the LLM-estimated grams for the same ingredient+measurement signature.
        if not usda_id:
            llm_weight_fallback_attempted = True
            llm_weight_fallback = _lookup_recipe1m_llm_weight_fallback(
                name=name,
                measurement=measurement,
                qty=qty,
                unit=unit,
            )
            if llm_weight_fallback is not None:
                grams = float(llm_weight_fallback["grams"])
                plausibility_error = _llm_weight_plausibility_error(
                    name=name,
                    qty=qty,
                    unit=unit,
                    grams=grams,
                )
                if plausibility_error is None:
                    portion_match = {
                        "portion_desc": "LLM weight fallback",
                        "grams_per_unit": None,
                    }
                    weights.append(float(grams))
                    details.append({
                        "name": name,
                        "measurement_raw": measurement,
                        "parsed_quantity": qty,
                        "parsed_unit": unit,
                        "quantity_inferred": qty_inferred,
                        "unit_inferred": unit_inferred,
                        "usda_id": None,
                        "food_group": None,
                        "usda_match_source": None,
                        "usda_match_similarity": None,
                        "usda_match_collection": None,
                        "usda_match_canonical": None,
                        "portion_match": portion_match,
                        "match_type": RECIPE1M_LLM_WEIGHT_MATCH_TYPE,
                        "weight_grams": float(grams),
                        "error": None,
                        "llm_fallback": True,
                        "llm_fallback_note": RECIPE1M_LLM_WEIGHT_NOTE,
                        "llm_fallback_source_file": str(RECIPE1M_LLM_WEIGHT_CSV_PATH),
                        "llm_fallback_ingredient": llm_weight_fallback.get("ingredient"),
                        "llm_fallback_sample_measurement": llm_weight_fallback.get("sample_measurement"),
                        "llm_fallback_signature": llm_weight_fallback.get("signature"),
                    })
                    continue
            llm_weight_fallback_failed = True

        if not usda_id or qty is None or unit is None:
            if not usda_id:
                error = "missing_usda_id"
            elif qty is None:
                error = "missing_quantity"
            else:
                error = "missing_unit"

            live_llm_grams = None
            live_llm_error = None
            if error in {"missing_unit", "missing_usda_id", "missing_quantity"}:
                # A qty of None (not just an unknown unit) gives the LLM
                # nothing to scale against — it reasonably returns 0 rather
                # than guess a count, which then fails the non-positive-
                # weight check below and falls through to a hard 0 g.
                # Assume a single serving, same as the unit-inference
                # default a few lines up (qty="1" when only a unit could be
                # inferred).
                live_llm_qty = qty if qty is not None else "1"
                live_llm_grams, live_llm_error, live_llm_unit = _live_llm_weight_fallback(
                    name=name,
                    qty=live_llm_qty,
                    unit=unit,
                )
                if live_llm_grams is not None:
                    # Checked against live_llm_unit (what the LLM actually
                    # used), not `unit` — `unit` is None here by definition
                    # (that's why this branch was reached), which always
                    # reads as "missing_unit" and rejected every estimate.
                    plausibility_error = _llm_weight_plausibility_error(
                        name=name,
                        qty=qty,
                        unit=live_llm_unit,
                        grams=float(live_llm_grams),
                        food_group=food_group,
                    )
                    if plausibility_error is not None:
                        live_llm_grams = None
                        live_llm_error = plausibility_error
                if live_llm_grams is not None:
                    if error == "missing_unit":
                        live_reason = "missing_unit"
                    elif error == "missing_usda_id":
                        live_reason = "missing_usda_id"
                    else:
                        live_reason = "missing_quantity"
                    weights.append(float(live_llm_grams))
                    details.append({
                        "name": name,
                        "measurement_raw": measurement,
                        "parsed_quantity": qty,
                        "parsed_unit": unit,
                        "quantity_inferred": qty_inferred,
                        "unit_inferred": unit_inferred,
                        "usda_id": usda_id,
                        "food_group": food_group,
                        "usda_match_source": usda_match_source,
                        "usda_match_similarity": usda_match_similarity,
                        "usda_match_collection": usda_match_collection,
                        "usda_match_canonical": None if not link else link.get("canonical"),
                        "portion_match": {
                            "portion_desc": "live LLM fallback",
                            "grams_per_unit": None,
                        },
                        "match_type": (
                            "live_llm_missing_unit_fallback"
                            if live_reason == "missing_unit"
                            else (
                                "live_llm_missing_usda_fallback"
                                if live_reason == "missing_usda_id"
                                else "live_llm_missing_quantity_fallback"
                            )
                        ),
                        "weight_grams": float(live_llm_grams),
                        "error": None,
                        "live_llm_fallback": True,
                        "live_llm_reason": live_reason,
                    })
                    qty_value = _parse_quantity_value(qty) or 1.0
                    _record_live_llm_resolution(
                        name=name,
                        unit=live_llm_unit,
                        grams_per_unit=float(live_llm_grams) / qty_value,
                        usda_id=usda_id,
                    )
                    continue

            weights.append(0.0)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": unit_inferred,
                "usda_id": usda_id,
                "food_group": food_group,
                "usda_match_source": usda_match_source,
                "usda_match_similarity": usda_match_similarity,
                "usda_match_collection": usda_match_collection,
                "usda_match_canonical": None if not link else link.get("canonical"),
                "portion_match": None,
                "match_type": None,
                "weight_grams": None,
                "error": error,
                "llm_weight_fallback_attempted": llm_weight_fallback_attempted,
                "llm_weight_fallback_failed": llm_weight_fallback_failed,
                "llm_weight_fallback_error": (
                    "no_match_for_ingredient_measurement_signature"
                    if llm_weight_fallback_failed
                    else None
                ),
                "llm_fallback_note": (
                    RECIPE1M_LLM_WEIGHT_NOTE
                    if llm_weight_fallback_attempted
                    else None
                ),
                "llm_fallback_source_file": (
                    str(RECIPE1M_LLM_WEIGHT_CSV_PATH)
                    if llm_weight_fallback_attempted
                    else None
                ),
                "live_llm_fallback": False,
                "live_llm_error": live_llm_error,
            })
            continue

        grams = None
        try:
            grams, portion_match, match_type = _estimate_grams_from_usda_id(
                name=name,
                qty=qty,
                unit=unit,
                usda_id=str(usda_id),
            )
        except ValueError:
            # If direct canonical link exists but doesn't support this unit,
            # try embedding fallback to recover a better USDA id.
            if usda_match_source == "direct":
                emb_link = _embedding_usda_link(name, unit=unit)
                emb_usda_id = (
                    None if not emb_link else str(emb_link.get("usda_id") or "").strip()
                )
                if emb_usda_id and emb_usda_id != str(usda_id):
                    try:
                        grams, portion_match, match_type = _estimate_grams_from_usda_id(
                            name=name,
                            qty=qty,
                            unit=unit,
                            usda_id=emb_usda_id,
                        )
                        link = emb_link
                        usda_id = emb_usda_id
                        usda_match_source = str(emb_link.get("match_source") or "embedding")
                        usda_match_similarity = emb_link.get("hybrid_score") or emb_link.get("similarity")
                        usda_match_collection = emb_link.get("match_collection")
                    except ValueError:
                        grams = None
            # Final fallback: estimate using the USDA weights index by name+unit.
            if grams is None:
                try:
                    grams, portion_match, name_match_usda_id = _estimate_grams_from_name_portion(
                        name=name,
                        qty=qty,
                        unit=unit,
                    )
                    match_type = "weight_name_portion_fallback"
                    if name_match_usda_id:
                        usda_id = name_match_usda_id
                        usda_match_source = "weight_name_fallback"
                        usda_match_similarity = None
                        usda_match_collection = "usda_weights_json"
                        link = usda_id_to_link(usda_id)
                        food_group = _food_group_for_link(link, usda_id)
                except ValueError:
                    grams = None
            if grams is None:
                llm_portion = _lookup_recipe1m_llm_portion_fallback(
                    name=name,
                    unit=unit,
                    usda_id=str(usda_id),
                    measurement=measurement,
                )
                qty_value = _parse_quantity_value(qty)
                if llm_portion is not None and qty_value is not None:
                    grams_per_unit = float(llm_portion["grams_per_unit"])
                    plausibility_error = _llm_portion_plausibility_error(
                        name=name,
                        unit=unit,
                        grams_per_unit=grams_per_unit,
                        food_group=food_group,
                    )
                    if plausibility_error is None:
                        grams = float(qty_value) * grams_per_unit
                        portion_match = {
                            "portion_desc": f"{_clean_unit(str(unit or ''))} (LLM per-unit fallback)",
                            "grams_per_unit": grams_per_unit,
                            "source_food_name": llm_portion.get("ingredient"),
                            "source": "llm_per_unit_fallback",
                            "sample_measurement": llm_portion.get("sample_measurement"),
                        }
                        match_type = RECIPE1M_LLM_PORTION_FALLBACK_MATCH_TYPE
                    else:
                        portion_match = {
                            "portion_desc": f"{_clean_unit(str(unit or ''))} (LLM per-unit fallback rejected)",
                            "grams_per_unit": grams_per_unit,
                            "source_food_name": llm_portion.get("ingredient"),
                            "source": "llm_per_unit_fallback",
                            "sample_measurement": llm_portion.get("sample_measurement"),
                            "rejected_reason": plausibility_error,
                        }
            if grams is None:
                error = "missing_portion_for_unit"

        if grams is None and error == "missing_portion_for_unit":
            live_llm_grams, live_llm_error, live_llm_unit = _live_llm_weight_fallback(
                name=name,
                qty=qty,
                unit=unit,
            )
            if live_llm_grams is not None:
                plausibility_error = _llm_weight_plausibility_error(
                    name=name,
                    qty=qty,
                    unit=live_llm_unit,
                    grams=float(live_llm_grams),
                    food_group=food_group,
                )
                if plausibility_error is not None:
                    live_llm_grams = None
                    live_llm_error = plausibility_error
            if live_llm_grams is not None:
                grams = float(live_llm_grams)
                portion_match = {
                    "portion_desc": "live LLM fallback",
                    "grams_per_unit": None,
                }
                match_type = "live_llm_missing_portion_fallback"
                error = None
                qty_value = _parse_quantity_value(qty) or 1.0
                _record_live_llm_resolution(
                    name=name,
                    unit=live_llm_unit,
                    grams_per_unit=grams / qty_value,
                    usda_id=usda_id,
                )
            else:
                # Keep original error and expose why live fallback failed.
                if live_llm_error:
                    error = f"missing_portion_for_unit ({live_llm_error})"

        weights.append(float(grams) if grams is not None else 0.0)
        details.append({
            "name": name,
            "measurement_raw": measurement,
            "parsed_quantity": qty,
            "parsed_unit": unit,
            "quantity_inferred": qty_inferred,
            "unit_inferred": unit_inferred,
            "usda_id": usda_id,
            "food_group": food_group,
            "usda_match_source": usda_match_source,
            "usda_match_similarity": usda_match_similarity,
            "usda_match_collection": usda_match_collection,
            "usda_match_canonical": None if not link else link.get("canonical"),
            "portion_match": portion_match,
            "match_type": match_type,
            "weight_grams": float(grams) if grams is not None else None,
            "error": error,
            "live_llm_fallback": bool(match_type and str(match_type).startswith("live_llm_")),
        })
    for idx in blank_salt_indexes:
        details[idx]["blank_quantity_policy"] = "salt_pinch"
    _apply_whole_bird_edible_yield(names_list, details, weights)
    _apply_low_confidence_live_llm(details, weights)
    _apply_blank_quantity_defaults(names_list, measures_list, details, weights)
    if return_details:
        if debug:
            return {"weights": weights, "details": details}
        return {"weights": weights, "details": [_compact_detail(d) for d in details]}
    return weights

    
def Ingredient_Weight_Node(state: RecipeState) -> RecipeState:
    debug = bool(state.debug)

    names = state.ingredient_names or []
    measurements = state.measurements or []

    # Weights are region-independent: multi-region reprofiling pre-populates
    # them from the first region's run so this (LLM/matching-heavy) step runs
    # once per recipe instead of once per region.
    if (
        isinstance(state.weights, list)
        and state.weights
        and len(state.weights) == len(names)
    ):
        trace = dict(state.pipeline_trace or {})
        trace.setdefault("weight_calculation", {
            "weights": list(state.weights),
            "details": [],
            "reused_precomputed_weights": True,
        })
        state.pipeline_trace = trace
        return state
    result = ingredient_weight_tool_usda.invoke({
        "ingredient_names": names,
        "measurements": measurements,
        "return_details": True,
        "debug": debug,
    })
    if isinstance(result, dict):
        state.weights = result.get("weights", [])
    else:
        state.weights = result

    trace = dict(state.pipeline_trace or {})
    if isinstance(result, dict):
        weight_details = result.get("details", [])
        trace["weight_calculation"] = {
            "weights": result.get("weights", []),
            "details": weight_details,
            "matched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is not None
            ),
            "unmatched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is None
            ),
        }
    else:
        trace["weight_calculation"] = {"weights": result}
    state.pipeline_trace = trace

    if debug:
        print("\n[Ingredient_Weight_Node] Used USDA weights to estimate grams.")
        print("[Ingredient_Weight_Node] Updated State Keys:", list(state.model_dump().keys()))

    return state
