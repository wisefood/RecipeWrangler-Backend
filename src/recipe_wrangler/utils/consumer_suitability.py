"""Versioned composition rules for vegan, vegetarian and pescatarian views."""

from __future__ import annotations

import re
from typing import Any

from recipe_wrangler.utils.food_ontology import (
    DAIRY_PRODUCT_KEYWORDS,
    MEAT_PRODUCT_KEYWORDS,
    ALLERGEN_DETECTION_RULES,
    ALLERGEN_POSSIBLE_KEYWORDS,
)

SUITABILITY_CLASSIFICATION_VERSION = "vegan-vegetarian-v10"
ORIGIN_CLASSIFICATION_VERSION = "vegan-vegetarian-v1"
PESCATARIAN_CLASSIFICATION_VERSION = "pescatarian-v8"
SUPPORTED_CONSUMER_GROUPS = ("vegan", "vegetarian", "pescatarian")


def classification_version(group: str) -> str:
    """Keep independently reviewed composition policies on explicit versions."""
    return PESCATARIAN_CLASSIFICATION_VERSION if group == "pescatarian" else SUITABILITY_CLASSIFICATION_VERSION


def composition_policy_versions() -> dict[str,str]:
    """Runtime policy versions for rollout readiness and response consumers."""
    return {group:classification_version(group) for group in SUPPORTED_CONSUMER_GROUPS}

DEFINITION_SOURCES: dict[str, list[str]] = {
    "vegan": [
        "fooddrinkeurope_evu_joint_statement",
        "safe_vegan_standards",
    ],
    "vegetarian": ["fooddrinkeurope_evu_joint_statement"],
    "pescatarian": ["recipe_wrangler_pescatarian_composition_policy_v1"],
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
            "egg white",
            "egg yolk",
            "heavy whipping cream",
            "whipping cream",
            "cream cheese",
            "sour cream",
            "vanilla yogurt",
            "vanilla yoghurt",
            "colby jack cheese",
            "monterey jack cheese",
            "pepper jack",
            "pepper jack cheese",
            "dubliner cheese",
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

# This is the repository's composition policy, not a FATO certification or
# an allergen/cross-contact safety assessment. Unknown ingredients stay unknown.
GROUP_RULES["pescatarian"] = {
    "blocking_allergens": [],
    # Presence of milk/fish does not resolve the other components of a mixture.
    "positive_allergens": [],
    "blocking_keywords": [
        "beef", "pork", "bacon", "ham", "turkey", "chicken", "duck", "goose",
        "lamb", "mutton", "veal", "venison", "goat", "meat", "sausage",
        "pepperoni", "prosciutto", "pig", "porcine", "bovine", "lard",
        "tallow", "suet", "animal rennet",
        *(term for term in MEAT_PRODUCT_KEYWORDS if term not in {"liver", "kidney", "offal", "tripe"}),
    ],
    "positive_keywords": [
        "milk", "egg", "honey", "butter", "cheese", "cream", "yogurt", "yoghurt",
        "fish", "fish fillet", "fish steak", "salmon", "tuna", "cod", "haddock",
        "trout", "mackerel", "sardine", "anchovy", "shrimp", "prawn", "crab",
        "lobster", "mussel", "oyster", "clam", "scallop", "squid", "octopus",
        "fish gelatin", "fish gelatine", "fish collagen",
    ],
}

# Species names must work even when a new ingredient has no ontology link.
for _group in ("vegan", "vegetarian", "pescatarian"):
    _field = "positive_keywords" if _group == "pescatarian" else "blocking_keywords"
    GROUP_RULES[_group][_field] = list(dict.fromkeys(
        GROUP_RULES[_group][_field]
        + [term for allergen in ("fish", "crustacean_shellfish", "molluscs")
           for term in ALLERGEN_DETECTION_RULES[allergen]["keywords"]
           if term not in ALLERGEN_POSSIBLE_KEYWORDS.get(allergen, [])]
    ))
for _group in ("vegan", "vegetarian"):
    GROUP_RULES[_group]["blocking_keywords"].append("gelatine")

# Named cuts establish meat even when a species is omitted. Avoid generic
# "steak"/"burger": cauliflower steak and bean burgers are not meat evidence.
_NAMED_MEAT_CUTS = [
    "flank steak", "blade roast", "chuck roast", "rump steak", "ribeye",
    "rib eye steak", "t bone steak", "porterhouse steak", "hamburger meat",
]
for _group in SUPPORTED_CONSUMER_GROUPS:
    GROUP_RULES[_group]["blocking_keywords"].extend(_NAMED_MEAT_CUTS)

# Exceptions describe a complete named alternative, never an arbitrary clause
# containing "vegan" or "dairy-free" alongside independent animal ingredients.
_ALTERNATIVE_FOODS = (r"(?:milk|cream|yogurt|yoghurt|cheese|butter|beef|chicken|pork|meat|mince|sausages?|burgers?|hamburgers?|bacon|ham|fish(?:[ -]+fillets?)?|salmon|tuna|cod|shrimp|prawns?|eggs?|gelatin|gelatine|collagen|(?:(?:white|milk|dark)[ -]+)?chocolate|"
                      + '|'.join(re.escape(k).replace(r'\ ', r'[ -]+') for k in _NAMED_MEAT_CUTS) + ')')
_ALTERNATIVE_MODIFIERS = r"(?:(?:fresh|frozen|dried|canned|tinned|cooked|raw|smoked|unsweetened|sweetened|plain|smooth|crunchy|natural|organic|fortified|whole|skimmed|semi[ -]*skimmed|greek|strained|full[ -]*fat|reduced[ -]*fat|low[ -]*fat|light)[ -]+)*"
_COMMON_NAME_EXCLUSIONS = [
    rf"^{_ALTERNATIVE_MODIFIERS}(?:coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea|nut)(?:[ -]+(?:flavoured|flavored))?[ -]+(?:milk|cream|yogurt|yoghurt)(?:[ -]+powder)?$",
    r"^cream[ -]+of[ -]+coconut$",
    rf"^{_ALTERNATIVE_MODIFIERS}(?:non[ -]*dairy|dairy[ -]*free)[ -]+(?:milk|cream|yogurt|yoghurt|butter|cheese)$",
    rf"^{_ALTERNATIVE_MODIFIERS}(?:peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter$",
    r"^(?:vegetable|plant[ -]*based)[ -]+(?:suet|lard)$",
    r"^(?:(?:canned|tinned|cooked|dried|dry)[ -]+)*butter[ -]*beans?$",
    r"^(?:(?:red|white|canned|tinned|cooked|dried|dry)[ -]+)*kidney[ -]+beans?$",
    r"^(?:fresh[ -]+)?oyster[ -]+mushrooms?$",
    r"^(?:cashew|almond|soy|soya|oat)[ -]+cheese(?:[ -]+sauce)?$",
    r"^(?:cream[ -]+of[ -]+tartar|(?:cocoa|cacao|apple)[ -]+butter|beef[ -]+tomato(?:es)?)$",
]
VEGAN_NAME_EXCLUSIONS = [
    rf"^{_ALTERNATIVE_MODIFIERS}(?:vegan|plant[ -]*based)[ -]+{_ALTERNATIVE_FOODS}$",
    *_COMMON_NAME_EXCLUSIONS,
]
VEGETARIAN_NAME_EXCLUSIONS = [
    rf"^{_ALTERNATIVE_MODIFIERS}(?:vegan|vegetarian|plant[ -]*based)[ -]+{_ALTERNATIVE_FOODS}$",
    *_COMMON_NAME_EXCLUSIONS,
    r"^(?:goat(?:s|['’]s)?|sheep(?:s|['’]s)?)[ -]+(?:milk|cheese|yogurt|yoghurt|feta)$",
]

PESCATARIAN_NAME_EXCLUSIONS = [
    r"^(?:vegan|vegetarian|plant[ -]*based)[ -]+(?:beef|chicken|pork|meat|mince|sausages?|burgers?|bacon|ham)$",
    r"^(?:(?:red|white|canned|tinned|cooked|dried|dry)[ -]+)*kidney[ -]+beans?$",
    r"^beef[ -]+tomato(?:es)?$",
    r"^(?:goat(?:s|['’]s)?|sheep(?:s|['’]s)?)[ -]+(?:milk|cheese|yogurt|yoghurt|feta)$",
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
 r"sweetened coconut", r"(?:earth balance[ -]+)?vegan[ -]+butter",
 r"popcorn", r"roasted (?:unsalted )?peanuts?", r"mini peppers?",
 r"gluten[ -]*free (?:baking )?flour",
 r"cane sugar", r"jicama", r"coffee", r"tea", r"brewed coffee", r"brewed tea",
 r"(?:(?:unsweetened|sweetened)[ -]+)?(?:vanilla(?:[ -]+flavo(?:u)?red)?[ -]+)?(?:soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)[ -]?milk",
 # seasonings and pantry
 r"salt", r"pepper(?:corns?)?", r"water", r"ice(?: cubes?)?", r"sugar", r"brown sugar", r"maple syrup", r"vinegar", r"(?:balsamic|red wine|white wine|cider|rice wine|malt|sherry) vinegar",
 r"baking (?:powder|soda)", r"bicarbonate of soda", r"bread soda", r"(?:dry |instant |active )?yeast", r"(?:corn ?flour|corn ?starch|cornstarch)", r"flour", r"(?:plain|self[- ]raising|wholemeal|strong|rye|spelt|bread) flour",
 r"cocoa(?: powder)?", r"vanilla (?:extract|essence)", r"mustard(?: seeds?| powder)?", r"dijon mustard", r"soy sauce", r"tomato (?:paste|puree|purée|sauce|passata)", r"passata", r"tahini", r"(?:olive|vegetable|sunflower|canola|rapeseed|sesame|coconut|groundnut|peanut|corn) oil",
 # herbs and spices
 r"basil", r"coriander(?: leaves| seeds)?", r"cilantro", r"parsley", r"mint(?: leaves)?", r"thyme", r"rosemary", r"oregano", r"sage", r"dill", r"chives?", r"bay leaves?", r"tarragon", r"marjoram",
 r"cumin(?: seeds)?", r"paprika", r"cinnamon", r"nutmeg", r"turmeric", r"cardamom", r"cloves?", r"allspice", r"cayenne(?: pepper)?", r"chilli(?: flakes| powder)?", r"chili(?: flakes| powder)?", r"curry powder", r"garam masala", r"mixed herbs", r"mixed spice", r"fennel seeds?", r"caraway seeds?", r"sesame seeds?", r"poppy seeds?", r"chia seeds?", r"sunflower seeds?", r"pumpkin seeds?", r"pepitas", r"linseeds?", r"flax ?seeds?",
 # vegetables and fruit
 r"tomato(?:es)?", r"onions?", r"spring onions?", r"scallions?", r"shallots?", r"garlic(?: cloves?)?", r"ginger", r"leeks?", r"potato(?:es)?", r"sweet potato(?:es)?", r"kumara", r"carrots?", r"celery", r"courgettes?", r"zucchinis?", r"(?:bell )?peppers?", r"capsicums?", r"broccoli", r"broccolini", r"cauliflower", r"cabbage", r"spinach", r"kale", r"silverbeet", r"chard", r"lettuce", r"rocket", r"arugula", r"cucumbers?", r"radish(?:es)?", r"mushrooms?", r"peas", r"sugar snap peas", r"snow peas", r"green beans", r"sweetcorn", r"corn", r"pumpkin", r"butternut squash", r"squash", r"beetroot", r"beets?", r"eggplant", r"aubergine", r"avocados?", r"asparagus", r"fennel", r"celeriac", r"parsnips?", r"turnips?", r"bok choy", r"pak choi",
 r"apples?", r"bananas?", r"lemons?", r"limes?", r"oranges?", r"mandarins?", r"strawberr(?:y|ies)", r"blueberr(?:y|ies)", r"raspberr(?:y|ies)", r"blackberr(?:y|ies)", r"cranberr(?:y|ies)", r"mango(?:es|s)?", r"pineapple", r"peach(?:es)?", r"pears?", r"grapes?", r"dates?", r"raisins?", r"sultanas?", r"apricots?", r"plums?", r"cherr(?:y|ies)", r"kiwi ?fruits?", r"melon", r"watermelon", r"passion ?fruit", r"figs?", r"prunes?",
 r"(?:lemon|lime|orange) (?:juice|zest)", r"(?:lemon|lime|orange) (?:juice and zest|zest and juice)",
 # grains, legumes, nuts
 r"rice", r"oats", r"rolled oats", r"quinoa", r"couscous", r"barley", r"bulgur", r"lentils", r"red lentils", r"chickpeas", r"kidney beans", r"black beans", r"cannellini beans", r"borlotti beans", r"baked beans", r"beans", r"edamame(?: beans)?", r"tofu", r"tempeh",
 r"almonds?", r"walnuts?", r"cashews?", r"peanuts?", r"pecans?", r"hazelnuts?", r"pistachios?", r"pine nuts?", r"brazil nuts?", r"desiccated coconut", r"coconut", r"coconut milk", r"nut milk", r"cream of coconut", r"mixed nuts",
]
# After a plant food these words name another dish ("garlic bread", "carrot cake", "onion beef"); they stay valid before it.
TRAILING_MODS = MODS.replace("|bread", "").replace("|cake", "").replace("|beef", "")
def pattern(noun): return rf"^(?:{MODS}[ ,-]+)*(?:{noun})(?:[ ,-]+(?:and|or|,)?[ ,-]*{TRAILING_MODS})*[ ,]*$"
PLANT_STAPLE_PATTERNS=[pattern(n) for n in NOUNS]
# Owner decision 7 Nov 2026: plain distilled spirits, dry pasta shapes and plain bread count as suitable by name.
# Liqueurs, creamy drinks, tortillas, mayonnaise, fresh/filled pasta and flavoured breads stay unknown. Not a product
# certification: fining, enrichment and cross-contact are not tracked in a name.
_SPIRIT = r"(?:(?:agave|blanco|reposado|silver|gold|spiced|dark|white|light|london dry|dry) )*(?:tequila|rum|vodka|gin|whisk(?:e)?y|bourbon|scotch|brandy|cognac|mezcal)"
_PASTA = r"(?:(?:dried|dry|uncooked|cooked|whole[ -]?wheat|wholemeal|whole[ -]?grain) )*(?:pasta|spaghetti|penne|fusilli|macaroni|linguine|tagliatelle|rigatoni|farfalle|orzo|vermicelli)"
_BREAD = r"(?:(?:white|brown|wholemeal|whole[ -]?wheat|rye|seeded|multigrain|sourdough|crusty|sliced|stale|day[ -]old|hot) )*(?:bread|sourdough bread|toast)"
PLANT_STAPLE_PATTERNS += [rf"^{x}$" for x in (_SPIRIT, _PASTA, _BREAD)]


def keyword_pattern(keyword: str) -> str:
    """Match whole food words, including consonant-y plurals such as anchovies."""
    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"[\s-]+")
    if len(keyword) > 1 and keyword.endswith('y') and keyword[-2] not in 'aeiou':
        escaped = escaped[:-1] + r"(?:y|ies)"
    else:
        escaped += r"(?:e?s)?"
    return rf"\b{escaped}\b"


def keyword_regex(keyword: str) -> str:
    """Return a case-insensitive-ready Neo4j/Python word-boundary pattern."""
    return rf".*{keyword_pattern(keyword)}.*"


def positive_keyword_regex(keyword: str) -> str:
    """A whole food/alternative claim can support an ingredient, not a mixture."""
    if keyword in {"vegan", "vegetarian", "plant based", "plant-based"}:
        cue = r"plant[ -]*based" if keyword.startswith("plant") else re.escape(keyword)
        return rf"^{_ALTERNATIVE_MODIFIERS}{cue}[ -]+{_ALTERNATIVE_FOODS}$"
    suffix = r"(?:[ -]+cheese)?" if keyword in DAIRY_PRODUCT_KEYWORDS else ""
    return rf"^{_ALTERNATIVE_MODIFIERS}" + keyword_regex(keyword).replace('.*', '') + suffix + "$"


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


def dietary_keyword_evidence(name: str, group: str) -> tuple[list[str], str]:
    """Apply food exclusions and optional/alternative scope before rejecting a diet."""
    from recipe_wrangler.utils.ingredient_context import contextual_hits, mask_food_lookalikes, remove_negated_keyword
    from recipe_wrangler.utils.dietary_ingredient_evidence import ingredient_diet_evidence

    # Protect complete food phrases before conjunction splitting (e.g. oyster
    # and shiitake mushrooms or vegan mac and cheese), without hiding the tail.
    if group == 'vegan':
        name = mask_food_lookalikes(name, 'milk')
    name = mask_diet_alternatives(name, group)
    for category in ('meat', 'seafood', 'fish'):
        name = mask_food_lookalikes(name, category)
    if group != 'vegan':
        name = mask_food_lookalikes(name, 'dairy_species')

    exclusions = {"vegan": VEGAN_NAME_EXCLUSIONS,
                  "vegetarian": VEGETARIAN_NAME_EXCLUSIONS,
                  "pescatarian": PESCATARIAN_NAME_EXCLUSIONS}[group]

    def find_hits(clause: str) -> list[str]:
        clause = clause.strip()
        clause = re.sub(r"^(.+),\s*((?:dairy|gluten)[ -]*free|vegan|vegetarian)\s*$", r"\2 \1", clause)
        if re.match(r"^milk,\s*(?:soya?|almond|oat)\b", clause):
            return []
        if any(re.search(pattern, clause) for pattern in exclusions):
            return []
        # Inverted commodity names retain their biological qualifiers.
        if re.match(r"eggs?,\s*chicken\b", clause):
            clause = re.sub(r"\bchicken\b", "", clause)
        hits = [keyword for keyword in GROUP_RULES[group]["blocking_keywords"]
                if re.search(keyword_pattern(keyword),
                             remove_negated_keyword(clause, keyword_pattern(keyword)))]
        evidence = ingredient_diet_evidence(clause, group)
        if evidence and evidence['status'] == 'not_suitable':
            hits.append('open_food_facts:' + evidence['ingredient_id'])
        # An unqualified ingredient named hamburger means the meat, whereas
        # hamburger buns, vegan hamburgers and unspecified burgers do not.
        if re.fullmatch(r'(?:(?:raw|ground|lean|extra lean)\s+)*hamburger', clause):
            hits.append('hamburger')
        return hits

    return contextual_hits(name, find_hits)


_CLAIMED_FOOD_MODIFIERS = r"(?:(?:hard|soft|plant|basil|cocktail|grated|shredded|smoked|unsalted)[ -]+)*"
_CLAIMED_FOOD_HEADS = (
    r"(?:mac(?:aroni)?[ -]+and[ -]+cheese|"
    + '|'.join(re.escape(k).replace(r'\ ', r'[ -]+') + r'(?:[ -]+cheese)?'
               for k in sorted(DAIRY_PRODUCT_KEYWORDS + MEAT_PRODUCT_KEYWORDS, key=len, reverse=True))
    + r"|pesto|" + _ALTERNATIVE_FOODS + r")"
)
_CLAIMED_FOOD_PATTERNS = {
    group: re.compile(r"\b(?:vegan|plant[ -]*based" +
                      (r"|vegetarian" if group != 'vegan' else '') +
                      r")[ -]+" + _CLAIMED_FOOD_MODIFIERS + _CLAIMED_FOOD_HEADS + r"\b")
    for group in SUPPORTED_CONSUMER_GROUPS
}
_MEAT_FREE_FOOD = re.compile(r"\bmeat[ -]*free[ -]+(?:cocktail[ -]+)?(?:sausages?|burgers?|mince|meatballs?)\b")


def mask_diet_alternatives(text: str, group: str) -> str:
    """A claim qualifies its adjacent food, not other foods in a recipe/title."""
    from recipe_wrangler.utils.ingredient_context import mask_food_spans

    text = text.casefold()
    text = re.sub(r"\b(milk|cream|cheese|butter|yogurt|yoghurt),\s*(vegan|vegetarian)\b",
                  r"\2 \1", text)
    text = mask_food_spans(text, _CLAIMED_FOOD_PATTERNS[group])
    return mask_food_spans(text, _MEAT_FREE_FOOD)


def dietary_title_evidence(title: str, group: str) -> tuple[list[str], str]:
    """A generic ice-cream dish title does not establish a dairy ingredient."""
    if group == 'vegan':
        title = re.sub(r'\bice[ -]+cream\b', 'frozen dessert', title, flags=re.I)
    return dietary_keyword_evidence(title, group)
