"""Small, conservative ingredient-clause helpers shared by composition rules."""
from __future__ import annotations

import re
from collections.abc import Callable


# Replace only a named food span, never its whole clause: independent animal
# ingredients and warning declarations must survive an alternative-food claim.
_DAIRY_LOOKALIKES = re.compile(
    r"\b(?:(?:coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea|nut|plant(?:[ -]*based)?)"
    r"(?:[ -]+(?:flavoured|flavored))?[ -]+(?:milk|cream|yogurt|yoghurt|cheese|butter)"
    r"|(?:non[ -]*dairy|dairy[ -]*free)[ -]+(?:alternative[ -]+to[ -]+)?"
    r"(?:(?:soft|hard|grated|shredded|unsweetened|sweetened|plain)[ -]+)*"
    r"(?:milk|cream|yogurt|yoghurt|butter|cheese|cr[eè]me[ -]+fra[iî]che|(?:white[ -]+)?chocolate)"
    r"|white[ -]+chocolate[ -]*\(?(?:non[ -]*dairy|dairy[ -]*free|milk[ -]*free)\)?"
    r"|(?:milk|cream|yogurt|yoghurt)[ -]+(?:alternative|substitute)"
    r"|(?:peanut|almond|cashew|hazelnut|walnut|seed|nut|pistachio|sunflower(?:[ -]+seed)?|pumpkin(?:[ -]+seed)?|sesame)"
    r"(?:[ -]*(?:&|and)[ -]+(?:cardamom|cinnamon))?[ -]+butter"
    r"|butter[ -]*beans?|cream[ -]+of[ -]+(?:tartar|coconut)|(?:cocoa|cacao|apple)[ -]+butter)\b"
)
_MEAT_LOOKALIKES = re.compile(
    r"\b(?:kidney[ -]+beans?|beef[ -]+tomato(?:es)?)\b"
)
_DAIRY_SPECIES_LOOKALIKES = re.compile(
    r"\b(?:goat(?:s|['’]s)?|sheep(?:s|['’]s)?)(?=[ -]+(?:milk|cheese|yogurt|yoghurt|feta|curd|kefir)\b)"
)
_MUSHROOM_LOOKALIKES = re.compile(
    r"\boyster(?:[ -]+and[ -]+(?:shiitake|button|chestnut|portobello|cremini))?[ -]+mushrooms?\b"
    r"|\bmushrooms?\b[^;()]*?\boyster\b(?![ -]+(?:sauce|shell))"
    r"|\boyster\s*,\s*mushrooms?\b"
)


_MAY_CONTAIN = re.compile(r"\b(?:may|might|could|can)\s+contain\b")
_PLANT_CRAB = re.compile(r"\b(?:vegan|vegetarian|plant[ -]*based)[ -]+(?:(?:imitation|artificial)[ -]+)?crab(?:meat|[ -]+meat)?\b")
_IMITATION_CRAB = re.compile(r"\b(?:imitation|artificial)[ -]+(crab(?:meat|[ -]+meat)?)\b")
_FISH_SAUCE_LOOKALIKES = re.compile(r"\b(?:vegan|vegetarian|fish[ -]*free|plant[ -]*based)[ -]+(?:nam[ -]+pla|nuoc[ -]+mam|fish[ -]+sauce)\b")
_PLANT_BUTTERED = re.compile(r"\b(?:(?:vegan|plant[ -]*based)[ -]+buttered[ -]+(?:toast|bread)|buttered[ -]+with[ -]+(?:vegan|plant[ -]*based)[ -]+butter)\b")
_AMOUNT_CHOICE = re.compile(r"\b\d+(?:[./]\d+)?\s+or\s+\d+(?:[./]\d+)?\b")
_FORM = r"(?:whole|slivered|chopped|sliced|diced|grated|ground|crushed|minced|flaked|raw|cooked)"
_FORM_CHOICE = re.compile(r"\(\s*"+_FORM+r"(?:\s+or\s+"+_FORM+r")+\s*\)")
_AMOUNT_PREFIX = r"^\s*[-*]*\s*(?:[\d¼½¾./ ]+\s*(?:lbs?|pounds?|oz|ounces?|kg|g|grams?|slices?)\.?\s*)?(?:deli\s+)?"
_CHEESE_COLOUR_CHOICE = re.compile(_AMOUNT_PREFIX+r"(?:white\s+or\s+yellow|yellow\s+or\s+white)(?=\s+(?:american\s+)?cheese\b)")
_FISH_CUT_CHOICE = re.compile(_AMOUNT_PREFIX+r"(?:(?:wild|fresh|frozen|smoked|skinless|boneless)\s+)*(?:salmon|tuna|cod|haddock|fish)\s+(?:fillets?|filets?)\s+or\s+steaks?\b(?=\s*(?:\.?$|:\s*\d))")


def _same_food_choices(text: str) -> str:
    """Normalize reviewed quantity/form choices without erasing food alternatives."""
    for pattern in (_AMOUNT_CHOICE, _FORM_CHOICE, _CHEESE_COLOUR_CHOICE, _FISH_CUT_CHOICE):
        text = pattern.sub(lambda match: re.sub(r"\bor\b", " ", match.group()), text)
    return text


def mask_food_spans(text: str, pattern: re.Pattern[str]) -> str:
    """Keep negated alternative claims from suppressing animal-food evidence."""
    def replace(match: re.Match[str]) -> str:
        if re.search(r"\b(?:not|non|isn['’]t)[ -]+(?:\w+[ -]+){0,2}$", text[:match.start()]):
            return match.group()
        return ' alternative_food '
    return pattern.sub(replace, text)


def mask_food_lookalikes(text: str, category: str) -> str:
    """Remove misleading food words only for the requested evidence category."""
    text = text.casefold()
    if category == 'milk':
        text = mask_food_spans(text, _PLANT_BUTTERED)
        text = re.sub(r"\bbeans?,\s*butter\b", 'butter beans', text)
        text = re.sub(r"\bmilk,\s*(soya?|almond|oat)\b", r"\1 milk", text)
        text = re.sub(r"\b(milk|cream|cheese|butter|yogurt|yoghurt),\s*"
                      r"((?:non[ -]*dairy|dairy[ -]*free))\b(?![ -]+alternative\b)", r"\2 \1", text)
        return mask_food_spans(text, _DAIRY_LOOKALIKES)
    if category == 'meat':
        return mask_food_spans(text, _MEAT_LOOKALIKES)
    if category == 'dairy_species':
        return mask_food_spans(text, _DAIRY_SPECIES_LOOKALIKES)
    if category in {'seafood', 'crustacean_formulation'}:
        text = mask_food_spans(mask_food_spans(text, _MUSHROOM_LOOKALIKES), _PLANT_CRAB)
        if category == 'crustacean_formulation':
            # Preserve exclusions while checking uncertain imitation-product composition.
            return _IMITATION_CRAB.sub(r'\1', text)
        return mask_food_spans(text, _IMITATION_CRAB)
    if category == 'fish':
        return mask_food_spans(text, _FISH_SAUCE_LOOKALIKES)
    raise ValueError(f'Unsupported food evidence category: {category}')


def ingredient_groups(name: str) -> list[tuple[list[str], bool]]:
    """Return independent groups of alternatives and their optional status.

    Keep commodity qualifiers (``cream, dairy-free``) attached to their food.
    This is deliberately lexical: it does not resolve missing product labels.
    """
    text = _same_food_choices(name.strip().casefold())
    # Parenthetical warnings are separate from the mandatory ingredient.
    text = re.sub(r"\(\s*((?:may|might|could|can|some\s+can)\s+contain\b[^)]*)\)", r"; \1", text)
    text = re.sub(r"\(\s*or\s+([^)]*)\)", r" or \1", text)
    # Commas inside preparation/quantity notes do not introduce another food.
    text = re.sub(r"\([^()]*\)", lambda match: re.sub(
        r",|\s+(?:and|with|plus)\s+|[;+]", " ", match.group()), text)
    text = re.sub(r"^beans,\s*butter\b", "butter beans", text)
    # These are inverted commodity names, not ingredient lists.
    if re.match(r"^(?:milk,\s*(?:soya?|almond|oat)\b|eggs?,\s*chicken\b)", text):
        return [([text], False)]
    # Split commas only when followed by another food, rather than a qualifier.
    qualifier = r"(?:optional(?=\s*$)|dollop|for|to\s+serve|drained|rinsed|grated|softened|finely|roughly|dairy[ -]*free|gluten[ -]*free|vegan|vegetarian|raw|cooked|peeled|chopped|diced|sliced|unsweetened|fortified|whole|scrambled|non[ -]*dairy|without|low[ -]*fat|reduced[ -]*fat)\b"
    text = re.sub(r",(?!\s*(?:" + qualifier + r"|\d{3}\b))\s*", "; ", text)
    clauses = re.split(r"(\s+(?:and|with|plus)\s+|[;+])", text)
    groups = []
    warning = False
    negated = False
    for clause in clauses:
        if re.fullmatch(r"\s+(?:and|with|plus)\s+|[;+]", clause):
            if clause.strip() != "and":
                negated = False
            continue
        clause = clause.strip()
        if not clause:
            continue
        if re.search(r"\b(?:contains?|with)\b", clause) and not _MAY_CONTAIN.search(clause):
            warning = negated = False
        warning = warning or bool(_MAY_CONTAIN.search(clause))
        if re.search(r"\bwithout\b", clause) and not re.search(r"\bnot\b.*\bwithout\b", clause):
            negated = True
        elif negated and not re.search(r"\bnot\b.*\bwithout\b", clause):
            clause = "without " + clause
        optional = warning or bool(re.search(r"\boptional\b", clause) and not re.search(
            r"\b(?:low[ -]+fat|reduced[ -]+fat)\s+is\s+optional\b", clause))
        groups.append((re.split(r"\bor\b", clause), optional))
        # A semicolon/comma ends a negative list; conjunctions can continue it.
        # Explicit contains/with above also resets negation.
    return groups


def contextual_hits(name: str, find_hits: Callable[[str], list[str]],
                    possible_keywords: set[str] | None = None) -> tuple[list[str], str]:
    """A mandatory group wins; a choice is definite only in every option."""
    possible_keywords = possible_keywords or set()
    # Resolve OR before conjunctions: in "chicken or tofu with milk", milk
    # belongs to one choice. Ambiguous shared modifiers stay conservative.
    name = _same_food_choices(name.casefold())
    choices = re.split(r"\bor\b", re.sub(r"\(\s*or\s+([^)]*)\)", r" or \1", name))
    if len(choices) > 1:
        results = [contextual_hits(choice, find_hits, possible_keywords) for choice in choices]
        hits = sorted({hit for found, _ in results for hit in found})
        return hits, ("contains" if all(found and state == "contains" for found, state in results)
                      else "possible")
    hits: set[str] = set()
    definite = False
    for options, optional in ingredient_groups(name):
        option_hits = [set(find_hits(option)) for option in options]
        hits.update(set().union(*option_hits))
        if not optional and all(found - possible_keywords for found in option_hits):
            definite = True
    return sorted(hits), "contains" if definite else "possible"


def remove_negated_keyword(text: str, pattern: str) -> str:
    if re.search(r"\bnot\b.*\bwithout\b", text):
        return text
    text = re.sub(r"\b(?:without|no(?:[\s-]+added)?)\s+(?:any\s+)?" + pattern, " ", text)
    return re.sub(pattern + r"[ -]*free\b", " ", text)
