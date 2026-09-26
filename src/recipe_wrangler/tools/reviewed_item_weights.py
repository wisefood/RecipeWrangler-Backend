"""Reviewed per-item / per-container weights for foods the USDA name lookup cannot resolve.

Each row is ``(name regex, {unit: grams}, source)``. ``source`` is ``USDA`` when the value is in the
local USDA SR portion table (data/processed/usda/usda-weights.json) and ``convention`` when it is a
typical retail or kitchen weight. Convention rows are deliberate, flagged fallbacks: they keep the
end user from seeing "0 g", they are not measurements.

Unit keys: ``each`` (bare count, whole, piece, size words; small/large scale by 0.65/1.35 unless the
row sets a size table), or a specific unit such as ``slice``, ``bunch``, ``handful``, ``tin``.
"""

from __future__ import annotations

import re
from typing import Optional

_SIZE_FACTORS = {"small": 0.65, "medium": 1.0, "large": 1.35}
_SIZE_WORDS = {"small", "medium", "large", "fat", "long", "big", "little"}
_EACH_UNITS = {
    "", "whole", "piece", "each", "fruit", "item", "unit", "cob", "spring", "pcs", "pc", "rib",
    "halved", "telegraph", "lebanese", "golden delicious", "golden", "medium-large", "delicious",
    "cutlet", "chop", "squash",
} | _SIZE_WORDS
_MEASURE_UNITS = {
    "g", "gram", "grams", "kg", "oz", "ounce", "lb", "pound", "pounds", "ml", "l", "litre", "liter",
    "cup", "cups", "tsp", "tbsp", "teaspoon", "tablespoon", "dessertspoon", "pint", "quart", "dl",
} | _SIZE_WORDS

_UNIT_ALIASES = {
    "dstspn": "dessertspoon", "links": "link", "pcs": "each", "ribs": "rib", "slices ": "slice",
    "handfull": "handful", "handfuls": "handful", "bunches": "bunch", "packets": "packet",
    "packages": "package", "packs": "pack", "bags": "bag", "tins": "tin", "cans": "can", "tubs": "tub",
    "pots": "pot", "pouches": "pouch", "slices": "slice", "stalks": "stalk", "ears": "ear",
    "pods": "pod", "florets": "floret", "sticks": "stick", "strips": "strip", "wrappers": "wrapper",
    "sheets": "sheet", "scoops": "scoop", "sprigs": "sprig", "leaves": "leaf", "leafs": "leaf",
    "drops": "drop", "few drops": "drop", "pieces": "piece", "balls": "ball", "shells": "shell",
    "cloves": "clove", "blocks": "block", "boxes": "box", "packages ": "package", "stems": "stem",
    "sausages": "each", "chips": "chip",
}

# (regex, {unit: grams}, source). "each" rows may add "sizes": {size: grams} via a tuple third element.
_ROWS: tuple = (
    # ---- fruit / veg counts -------------------------------------------------------------
    (r"\blychees?\b", {"each": 10.0}, "convention"),
    (r"\bclementines?\b", {"each": 74.0}, "USDA"),
    (r"\btamarillos?\b", {"each": 80.0}, "convention"),
    (r"\bfeijoas?\b", {"each": 40.0}, "convention"),
    (r"\bkiwi ?fruits?\b", {"each": 69.0}, "USDA"),
    (r"\bplantains?\b", {"each": 180.0}, "convention"),
    (r"\bjicama\b", {"each": 300.0}, "convention"),
    (r"\bkohlrabi\b", {"each": 190.0}, "convention"),
    (r"\bceleriac\b", {"each": 500.0}, "convention"),
    (r"\bwatermelon radish\b", {"each": 150.0}, "convention"),
    (r"\bbeet(?:s|root|roots)?\b(?!.*(?:canned|baby|bunch))", {"each": 82.0, "slice": 15.0, "pack": 250.0}, "USDA"),
    (r"\bcanned beet(?:root)?\b", {"slice": 15.0}, "convention"),
    (r"\bbaby beet(?:root)?s?\b", {"bunch": 300.0}, "convention"),
    (r"\bacorn squash(?:es)?\b", {"each": 400.0}, "convention"),
    (r"\bgem squash(?:es)?\b", {"each": 200.0}, "convention"),
    (r"\bsquash blossoms\b", {"each": 5.0}, "convention"),
    (r"\b(?:yellow )?squash(?:es)?\b(?!.*blossom)|\bscallopini\b", {"each": 196.0}, "convention"),
    (r"\b(?:courgettes?|zucchinis?)\b(?!.*ribbons)", {"each": 196.0, "pinch": 0.3}, "convention"),
    (r"\btelegraph\b|\bcucumber\b(?!.*lebanese)", {"each": 301.0}, "USDA"),
    (r"\blebanese\b.*\bcucumber\b|\bcucumber\b.*\blebanese\b", {"each": 100.0}, "convention"),
    (r"\bbroccolini\b", {"each": 25.0, "stalk": 25.0, "bunch": 170.0}, "convention"),
    (r"\basparagus\b", {"each": 16.0, "stalk": 16.0, "spear": 16.0}, "USDA"),
    (r"\bbroccoli florets?\b", {"each": 30.0}, "USDA"),
    (r"\bcauliflower head\b", {"each": 588.0}, "USDA"),
    (r"\bcauliflower\b", {"floret": 13.0, "each": 588.0}, "USDA"),
    (r"\bcorn husks?\b", {"each": 2.0}, "convention"),
    (r"\bcorn tacos?\b|\btaco shells?\b", {"each": 13.0}, "convention"),
    (r"\b(?:sweet )?corn\b(?!.*(?:flour|meal|starch|flakes|tortilla|husk|taco))", {"ear": 102.0, "each": 102.0}, "USDA"),
    (r"\bokra\b", {"pod": 11.9, "each": 11.9}, "USDA"),
    (r"\bgreen beans\b", {"each": 5.5, "bunch": 250.0, "handful": 60.0}, "USDA"),
    (r"\bspring onions?\b|\bscallions?\b|\bgreen onions?\b", {"each": 15.0, "stalk": 15.0, "spring": 15.0}, "USDA"),
    (r"\bred (?:chill?ies|chillis?)\b|\bchill?ies\b", {"each": 45.0}, "USDA"),
    (r"\bcapiscums?\b|\bcapsicums?\b|\bbell peppers?\b", {"each": 119.0}, "USDA"),
    (r"\bmushrooms?\b", {"slice": 5.0}, "convention"),
    (r"\bgarlic\b", {"clove": 3.0, "each": 5.0}, "USDA"),
    (r"\bginger\b", {"chunk": 20.0, "each": 15.0, "piece": 15.0}, "convention"),
    (r"\blemongrass\b", {"each": 20.0, "piece": 15.0, "stalk": 20.0}, "convention"),
    (r"\bpumpkin\b", {"can": 425.0}, "USDA"),
    (r"\bwatermelon\b", {"slice": 280.0}, "convention"),
    (r"\bbanana\b", {"slice": 8.0}, "convention"),
    (r"\bmandarin(?: oranges?)?\b", {"slice": 8.0}, "convention"),
    (r"\blemon\b", {"slice": 6.0}, "convention"),
    (r"\bcitrus fruit\b", {"each": 130.0}, "convention"),
    (r"\bapricots?\b", {"handful": 75.0}, "convention"),
    (r"\bpineapple\b", {"handful": 75.0}, "convention"),
    (r"\bgrapefruit\b", {"handful": 75.0}, "convention"),
    (r"\braspberries\b", {"each": 1.9, "handful": 75.0}, "USDA"),
    (r"\bblueberries\b", {"each": 1.4, "handful": 75.0}, "USDA"),
    (r"\bstrawberries\b", {"each": 18.0}, "USDA"),
    (r"\bgrapes\b", {"bunch": 150.0}, "convention"),
    (r"\balmonds\b", {"each": 1.2}, "USDA"),
    (r"\brhubarb\b", {"each": 51.0, "piece": 51.0, "stalk": 51.0}, "USDA"),
    # ---- lettuce / leaves / herbs -----------------------------------------------------
    (r"\bbaby (?:cos|gem|mignonette)\b.*\blettuces?\b|\blettuces?\b.*\bbaby\b|\blittle gem\b|\bbaby gem\b|\bmignonette\b", {"leaf": 5.0, "each": 150.0}, "convention"),
    (r"\bcrispy lettuce\b|\biceberg\b", {"leaf": 20.0, "each": 539.0, "sizes": {"small": 324.0, "medium": 539.0, "large": 755.0}}, "USDA"),
    (r"\b(?:cos|romaine)\b.*\blettuces?\b", {"leaf": 10.0, "each": 626.0, "noscale": 1}, "USDA"),
    (r"\b(?:head |butter(?:head)? )?lettuces?\b", {"leaf": 10.0, "each": 500.0, "sizes": {"small": 325.0, "medium": 500.0, "large": 700.0}}, "convention"),
    (r"\b(?:baby )?spinach\b|\bsalad leaves\b|\bmixed salad greens\b|\bsalad greens\b|\bmesclun\b", {"bag": 120.0, "packet": 120.0, "handful": 20.0}, "convention"),
    (r"\b(?:arugula|rocket)\b", {"handful": 20.0, "bag": 100.0}, "USDA"),
    (r"\bslaw mix\b|\bcoleslaw\b", {"handful": 50.0, "packet": 250.0, "pack": 250.0}, "convention"),
    (r"\bstir[- ]?fry vegetables\b", {"bag": 400.0}, "convention"),
    (r"\b(?:kale|cavolo nero|chard|silver ?beet|spinach)\b.*\bleaves\b", {"each": 10.0, "large": 15.0, "small": 6.0, "leaf": 10.0}, "convention"),
    (r"\b(?:gai lan|chinese broccoli|cavolo nero|silver ?beet|swiss chard)\b", {"bunch": 250.0, "each": 100.0, "large": 150.0}, "convention"),
    (r"\bbok choy\b|\bpak choi\b", {"pack": 300.0, "each": 120.0}, "convention"),
    (r"\b(?:baby |dutch )?carrots\b", {"bunch": 250.0}, "convention"),
    (r"\b(?:fresh )?(?:coriander|cilantro)\b", {"bunch": 30.0, "each": 30.0, "sprig": 1.0, "whole": 30.0}, "convention"),
    (r"\bparsley\b(?!.*root)", {"bunch": 30.0, "each": 30.0, "sprig": 1.0, "handful": 10.0}, "USDA"),
    (r"\b(?:mint|spearmint)\b", {"bunch": 30.0, "each": 1.0, "sprig": 1.0, "leaf": 0.15}, "USDA"),
    (r"\bbasil\b", {"bunch": 30.0, "each": 1.0, "sprig": 1.0, "leaf": 0.5}, "USDA"),
    (r"\bdill\b", {"bunch": 20.0, "each": 20.0, "sprig": 0.2}, "USDA"),
    (r"\bchives\b", {"handful": 10.0, "bunch": 20.0}, "convention"),
    (r"\bthyme\b", {"each": 0.5, "sprig": 0.5, "stalk": 0.5}, "convention"),
    (r"\brosemary\b", {"each": 2.0, "sprig": 2.0, "stem": 2.0, "stalk": 2.0}, "convention"),
    (r"\bmarjoram\b", {"sprig": 0.3}, "convention"),
    (r"\bherbs?\b|\bleaves or a sprig\b", {"bunch": 30.0, "leaf": 0.15, "each": 5.0}, "convention"),
    (r"\bbay leaf\b|\bbay leaves\b", {"each": 0.2}, "convention"),
    (r"\bsaffron\b", {"each": 0.1}, "convention"),
    (r"\bcloves\b", {"whole": 0.15, "each": 0.15}, "convention"),
    (r"\ballspice\b", {"whole": 0.1, "each": 0.1}, "convention"),
    (r"\bcardamoms?\b", {"whole": 0.2, "each": 0.2}, "convention"),
    (r"\bcinnamon\b", {"stick": 3.0}, "convention"),
    # ---- meat / fish -----------------------------------------------------------------
    (r"\bchicken thighs?\b", {"each": 193.0}, "USDA"),
    (r"\bchicken drumsticks?\b", {"each": 130.0}, "USDA"),
    (r"\bchicken wings?\b", {"each": 107.0}, "USDA"),
    (r"\bsmoked chicken breast\b|\bturkey\b", {"slice": 20.0}, "convention"),
    (r"\blamb shanks?\b", {"each": 200.0}, "convention"),
    (r"\blamb steaks?\b", {"each": 200.0, "steak": 200.0}, "convention"),
    (r"\blamb leg steaks?\b", {"each": 150.0}, "convention"),
    (r"\blamb\b", {"rack": 350.0}, "convention"),
    (r"\bpork (?:steaks?|loin chops?)\b", {"each": 185.0, "steak": 185.0}, "USDA"),
    (r"\bpork (?:cutlets?|schnitzels?)\b", {"each": 120.0}, "convention"),
    (r"\bpork medallions?\b", {"each": 80.0}, "convention"),
    (r"\bt[- ]bone steaks?\b", {"each": 360.0, "steak": 360.0}, "USDA"),
    (r"\b(?:beef )?sirloin steaks?\b", {"each": 200.0, "steak": 200.0}, "convention"),
    (r"\bfish (?:steaks?|fillets?)\b|\btilapia fillets?\b", {"each": 150.0, "steak": 180.0, "fillet": 150.0}, "convention"),
    (r"\bburger patt(?:y|ies)\b", {"each": 113.0}, "convention"),
    (r"\bham hocks?\b", {"each": 300.0}, "convention"),
    (r"\bpepperoni\b", {"slice": 2.0}, "USDA"),
    (r"\bpancetta\b", {"slice": 10.0}, "convention"),
    (r"\bsmoked salmon\b", {"slice": 30.0}, "convention"),
    (r"\bsmoked mackerel\b", {"packet": 200.0}, "convention"),
    (r"\bsmoked fish\b|\bchilli tuna\b", {"tin": 95.0, "can": 95.0}, "convention"),
    (r"\bsquid tubes?\b", {"each": 80.0}, "convention"),
    (r"\bmussels?\b", {"each": 12.0}, "convention"),
    (r"\bscallops?\b", {"shell": 20.0, "each": 20.0}, "convention"),
    (r"\bseaweed\b", {"strip": 1.0}, "convention"),
    # ---- bread / bakery / snacks -----------------------------------------------------
    (r"\bhamburger buns?\b|\bburger buns?\b|\bwholemeal buns?\b|\bbuns?\b", {"each": 44.0}, "USDA"),
    (r"\bbaguettes?\b|\bfrench bread\b", {"each": 250.0, "slice": 25.0, "long": 250.0, "sliceish": 1}, "convention"),
    (r"\bciabatta\b", {"each": 250.0, "loaf": 250.0}, "convention"),
    (r"\bpanini\b", {"each": 100.0}, "convention"),
    (r"\bpie crusts?\b", {"each": 180.0}, "convention"),
    (r"\bturkish (?:pide )?bread\b|\bpide\b", {"each": 250.0}, "convention"),
    (r"\bfrench bread sticks?\b", {"each": 60.0}, "convention"),
    (r"\bmountain bread\b", {"piece": 35.0}, "convention"),
    (r"\b(?:lebanese )?flatbreads?\b|\bpita bread\b", {"each": 60.0, "packet": 360.0}, "USDA"),
    (r"\bcob loaf\b|\bwholemeal or rye cobb\b", {"each": 400.0}, "convention"),
    (r"\bbread\b(?!.*(?:crumb|mix))", {"loaf": 680.0, "whole": 30.0, "slice": 30.0}, "convention"),
    (r"\bpoppadums?\b|\bpappadums?\b", {"each": 10.0}, "convention"),
    (r"\bpikelets?\b", {"each": 25.0}, "convention"),
    (r"\bmeringue(?: shells| nests)?\b|\bmeringues\b", {"each": 8.0}, "convention"),
    (r"\bm&m'?s\b", {"each": 1.0}, "convention"),
    (r"\blollies\b", {"each": 5.0}, "convention"),
    (r"\blicorice allsorts\b", {"each": 7.0}, "convention"),
    (r"\blicorice strap\b", {"each": 20.0}, "convention"),
    (r"\bmarshmallows\b", {"each": 7.0}, "convention"),
    (r"\bvanilla wafers\b", {"each": 4.0}, "convention"),
    (r"\bdark[- ]choc bits\b", {"each": 0.3}, "convention"),
    (r"\bpretzels\b", {"each": 5.0}, "convention"),
    (r"\bcannelloni tubes\b", {"each": 10.0}, "convention"),
    (r"\blasagn[ae] (?:noodles|sheets)\b", {"each": 20.0}, "convention"),
    (r"\b(?:rice paper|dumpling|gow gee|gyoza) wrappers?\b", {"each": 6.0, "wrapper": 6.0, "large": 8.0, "packet": 250.0}, "convention"),
    (r"\bwonton wrappers?\b", {"each": 8.0, "wrapper": 8.0}, "USDA"),
    (r"\b(?:pork |vegetable )?gyozas\b|\bfrozen dumplings\b", {"each": 25.0}, "convention"),
    (r"\bramen\b.*noodles|\binstant ramen\b", {"package": 85.0}, "USDA"),
    (r"\brice noodles\b", {"handful": 40.0}, "convention"),
    (r"\bcrispy noodles\b", {"handful": 20.0}, "convention"),
    # ---- dairy / packaged / containers -----------------------------------------------
    (r"\bice[- ]?cream\b|\bsorbet\b", {"scoop": 66.0}, "USDA"),
    (r"\bfeta\b", {"block": 200.0}, "convention"),
    (r"\bcream cheese\b", {"tub": 250.0}, "convention"),
    (r"\bstring cheese\b|\bcheese stick\b", {"stick": 28.0}, "convention"),
    (r"\bbocconcini\b", {"each": 30.0, "ball": 30.0}, "convention"),
    (r"\bpesto\b", {"tub": 190.0, "jar": 190.0}, "convention"),
    (r"\bpine nuts\b", {"packet": 50.0}, "convention"),
    (r"\bcouscous\b", {"packet": 250.0}, "convention"),
    (r"\bbrown rice\b|\brice & quinoa\b|\bsteamed rice\b", {"packet": 250.0, "pot": 250.0, "pouch": 250.0}, "convention"),
    (r"\bcrushed tomatoes\b|\bchopped tomatoes\b", {"tin": 400.0, "can": 400.0}, "convention"),
    (r"\bgravy mix\b", {"packet": 28.0}, "convention"),
    (r"\btaco seasoning\b", {"packet": 28.0}, "convention"),
    (r"\bsazon\b", {"package": 3.5}, "convention"),
    (r"\bcake mix\b", {"package": 430.0}, "convention"),
    # ---- second pass: remaining one-offs -----------------------------------------------
    (r"\bcucumber\b", {"each": 301.0}, "USDA"),
    (r"\bbacon\b", {"each": 25.0, "strip": 25.0, "slice": 25.0}, "convention"),
    (r"\bchicken breast fillets?\b", {"each": 174.0}, "USDA"),
    (r"\bcelery\b", {"rib": 40.0}, "convention"),
    (r"\bwalnuts?\b", {"each": 2.5}, "convention"),
    (r"\bsalmon darnes?\b", {"each": 175.0}, "convention"),
    (r"\bavocad(?:o|oe)s?\b", {"each": 150.0}, "USDA"),
    (r"\bpassion ?fruits?\b", {"each": 18.0}, "USDA"),
    (r"\bstar ?fruits?\b|\bcarambola\b", {"each": 91.0}, "USDA"),
    (r"\bcantaloupe\b|\brockmelon\b", {"each": 552.0}, "USDA"),
    (r"\bpaw ?paw\b|\bpapaya\b", {"each": 304.0}, "USDA"),
    (r"\bpeppadews?\b", {"each": 10.0}, "convention"),
    (r"\brye crispbreads?\b|\bcrispbreads?\b", {"each": 11.0}, "USDA"),
    (r"\bsquash blossoms\b", {"each": 5.0}, "convention"),
    (r"\bcheese spread triangle\b", {"each": 17.5}, "convention"),
    (r"\bcherries\b", {"each": 8.0}, "USDA"),
    (r"\bjelly sweets\b", {"each": 3.0}, "convention"),
    (r"\bjuniper berries\b", {"each": 0.2}, "convention"),
    (r"\b(?:black )?peppercorns\b", {"each": 0.05}, "convention"),
    (r"\bbouquet garni\b", {"bunch": 5.0}, "convention"),
    (r"\bfennel\b", {"sprig": 1.0}, "convention"),
    (r"\bmarjoram\b", {"bunch": 30.0}, "convention"),
    (r"\bcarrots?\b", {"slice": 5.0}, "convention"),
    (r"\bradish(?:es)?\b", {"slice": 1.0}, "USDA"),
    (r"\bkiwi fruit\b", {"slice": 10.0}, "convention"),
    (r"\bpineapple\b", {"slice": 84.0}, "USDA"),
    (r"\bchorizo\b|\bkolb[aá]sz\b|\bsausages?\b", {"each": 60.0, "link": 60.0}, "USDA"),
    (r"\bpizza (?:dough|base)s?\b", {"each": 250.0}, "convention"),
    (r"\bparsley root\b", {"each": 100.0}, "convention"),
    (r"\bwhey protein\b", {"scoop": 30.0}, "convention"),
    (r"\bnoodles\b.*\bnests?\b|\bwholewheat noodles\b", {"nest": 60.0}, "convention"),
    (r"\bcoconut milk\b", {"tin": 400.0, "can": 400.0}, "convention"),
    (r"\bmixed beans\b|\bchickpeas\b|\bkidney beans\b|\bbeans\b", {"tin": 400.0, "can": 400.0}, "convention"),
    (r"\btuna\b", {"tin": 120.0, "can": 120.0}, "convention"),
    (r"\b(?:chicken|mushroom)\b.*\bsoup\b", {"tin": 295.0, "can": 295.0}, "convention"),
    (r"\bvanillin sugar\b", {"pack": 8.0, "packet": 8.0}, "convention"),
    (r"\bbaking powder\b", {"packet": 12.0}, "convention"),
    (r"\b(?:natural )?yogh?urt\b", {"each": 150.0, "pot": 150.0}, "convention"),
    (r"\bsour cream\b", {"dollop": 20.0, "tub": 200.0}, "convention"),
    (r"\bbroccoli\b", {"each": 300.0, "stalk": 151.0, "floret": 30.0}, "convention (USDA bunch 608 is too large for one head)"),
    (r"\bcorn tortillas?\b", {"each": 26.0}, "USDA"),
    (r"\bcrusty bread\b|\bartisan bread\b", {"each": 400.0, "loaf": 400.0}, "convention"),
    (r"\bwholemeal bread\b|\bwhole wheat bread\b", {"each": 30.0}, "convention"),
    (r"\bstrawberries\b", {"each": 18.0}, "USDA"),
    (r"\bapples?\b", {"each": 182.0}, "USDA"),
    (r"\bhamburger buns?\b", {"each": 44.0}, "USDA"),
    (r"\bfish\b", {"steak": 180.0}, "convention"),
    (r"\bice\b", {"each": 25.0, "cube": 25.0}, "convention"),
    (r"\biceberg\b", {"bunch": 539.0}, "USDA"),
    (r"\bmaple syrup\b|\bhoney\b", {"drizzle": 10.0}, "convention"),
    (r"\boil\b", {"drizzle": 5.0}, "convention"),
    (r"\bwater\b", {"jug": 500.0}, "convention"),
    # ---- drops / pinches (any food) ---------------------------------------------------
    (r"\brosewater\b|\btabasco\b|\bhot pepper sauce\b|\bhot sauce\b", {"drop": 0.05}, "convention"),
)

_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), units, source) for pattern, units, source in _ROWS)

# Applies to any food when nothing specific matched (unit conventions, small amounts only).
GENERIC_UNIT_GRAMS = {"pinch": 0.3, "dash": 0.6, "drop": 0.05}

_LEAFY_HANDFUL = re.compile(r"spinach|arugula|rocket|salad|lettuce|greens|leaves|slaw|kale")
_NUT_HANDFUL = re.compile(r"nuts?|seeds?|almonds?|cashews?|walnuts?")


def _norm_unit(unit: Optional[str]) -> str:
    text = str(unit or "").strip().lower()
    return _UNIT_ALIASES.get(text, text)


def _each_units_match(unit: str) -> bool:
    return unit in _EACH_UNITS or bool(re.fullmatch(r"\d+(?:\.\d+)?[- ]?(?:inch|in)", unit))


def reviewed_default_unit(name: str) -> Optional[str]:
    """Unit to assume for a bare count of this food ("whole"), or None."""
    text = str(name or "").lower()
    for pattern, units, _ in _COMPILED:
        if pattern.search(text) and "each" in units:
            return "whole"
    return None


def reviewed_item_grams(
    name: str, unit: Optional[str], qty: float = 1.0, measurement: object = None
) -> Optional[tuple[float, str]]:
    """Return ``(grams_per_unit, label)`` for a reviewed count/container weight, or None."""
    text = str(name or "").lower()
    words = re.findall(r"[a-z]+", str(measurement or "").lower())
    candidates = _unit_candidates(unit)
    # The parsed unit can be just a size word ("small" of "4 small scoops"); try the last word too.
    if _norm_unit(unit) in _MEASURE_UNITS:
        words = []  # a real unit (g, ml, cup...) is never overridden by trailing measurement words
    for word in reversed(words):
        for candidate in _unit_candidates(word):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        found = _lookup(text, candidate, qty)
        if found is not None:
            return found
    return None


def _unit_candidates(unit: Optional[str]) -> list[str]:
    """The unit as written, then its singular, then its last word ("small scoops" -> "scoop")."""
    base = _norm_unit(unit)
    out = [base]
    for word in (base, base.split()[-1] if base.split() else base):
        for candidate in (word, _norm_unit(word), word[:-1] if word.endswith("s") else word):
            if candidate not in out:
                out.append(candidate)
    return out


def _lookup(text: str, unit_norm: str, qty: float) -> Optional[tuple[float, str]]:
    # "4oz" / "5cm-piece" style units carry their own size.
    mass = re.fullmatch(r"(\d+(?:\.\d+)?)\s*oz", unit_norm)
    if mass and re.search(r"fillet|steak|chicken|fish", text):
        return float(mass.group(1)) * 28.3495, "per-item ounces from the unit"
    if unit_norm in {"cm", "centimetre", "centimeter"}:
        per_cm = 0.6 if "cinnamon" in text else 5.0 if "ginger" in text else 1.5 if "lemongrass" in text else None
        if per_cm:
            return per_cm, f"{per_cm} g/cm (convention)"
    length = re.fullmatch(r"(\d+(?:\.\d+)?)\s*cm(?:[- ]?piece)?", unit_norm)
    if length:
        per_cm = 5.0 if "ginger" in text else 0.6 if "cinnamon" in text else 1.5 if "lemongrass" in text else None
        if per_cm:
            return float(length.group(1)) * per_cm, f"{per_cm} g/cm (convention)"
    for pattern, units, source in _COMPILED:
        if not pattern.search(text):
            continue
        if unit_norm in units:
            return units[unit_norm], f"reviewed {unit_norm} ({source})"
        if "each" in units and _each_units_match(unit_norm):
            base = units["each"]
            if "sliceish" in units and qty > 4:  # "12 whole" baguette bread means 12 slices, not 12 loaves
                return units["slice"], f"reviewed slice ({source})"
            if "leaf" in units and qty > 3 and re.search(r"lettuce", text):
                return units["leaf"] * (1.35 if unit_norm == "large" else 1.0), f"reviewed lettuce leaf ({source})"
            sizes = units.get("sizes")
            if sizes and unit_norm in sizes:
                return sizes[unit_norm], f"reviewed {unit_norm} ({source})"
            if unit_norm in _SIZE_FACTORS and not units.get("noscale"):
                if "leaf" in units and qty > 3 and re.search(r"lettuce", text):
                    return units["leaf"] * (1.35 if unit_norm == "large" else 1.0), f"reviewed lettuce leaf ({source})"
                return base * _SIZE_FACTORS[unit_norm], f"reviewed {unit_norm} ({source})"
            return base, f"reviewed each ({source})"
    if unit_norm == "handful":
        if _LEAFY_HANDFUL.search(text):
            return 20.0, "handful of leaves (convention)"
        if _NUT_HANDFUL.search(text):
            return 30.0, "handful of nuts/seeds (convention)"
        return 30.0, "handful (convention)"
    if unit_norm in GENERIC_UNIT_GRAMS:
        return GENERIC_UNIT_GRAMS[unit_norm], f"{unit_norm} (convention)"
    return None
