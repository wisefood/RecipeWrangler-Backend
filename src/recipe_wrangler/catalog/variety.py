"""Variety across a plan — because distinct ids are not distinct meals.

`plan_meals` already refuses to reuse a recipe: it accumulates a `used` set and
excludes it from every later slot, so a seven-day plan contains sixty-three
different `recipe_id`s. By that measure variety was perfect. This is what it
produced:

    Day 1  breakfast  Gluten-free berry muesli
    Day 2  breakfast  Nutty toasted muesli
    Day 3  breakfast  Gluten-free muesli
    Day 7  breakfast  Toasted muesli

Four different recipes, four different ids, one breakfast. The same week served
four salads for lunch and five roasted vegetable dishes. Nobody eating it would
call it a varied week, and the id check cannot see the problem because it is
looking at identity when the member is looking at *kind*.

The cause is not randomness, it is the opposite. Candidates come back in a
deliberately deterministic order — planning tier, then Nutri-Score, then source
— so that regenerating a plan does not reshuffle it. Recipes of a kind score
alike, so they arrive adjacent, and taking the top N of a sorted list takes them
as a block. Determinism is worth keeping; taking blindly from it is not.

So this module adds a second axis: pick the best candidate whose *dish family*
is not already on the plan, and only fall back to a repeat when the corpus has
nothing else to offer. The ranking still decides what is good — this only
decides when to skip down it.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Sequence

# Where a title stops naming the dish and starts listing what is in it.
#
# "Baked eggplant with cranberry and mint" is an eggplant dish; everything after
# "with" is garnish. Splitting here is what makes the last word the head noun
# rather than "mint".
#
# Commas are deliberately *not* split on: "Tomato, chickpea and barley salad"
# would become "Tomato", losing the one word that says what the dish is.
_TAIL = re.compile(r"\s+(?:with|on|served|topped|and served|in a|drizzled)\s+")

# Words that qualify a dish without naming one. Stripped so "Gluten-free berry
# muesli", "Nutty toasted muesli" and "Toasted muesli" reduce to the same head.
_QUALIFIERS: frozenset[str] = frozenset({
    "gluten", "free", "dairy", "sugar", "low", "fat", "light", "easy", "quick",
    "simple", "best", "homemade", "classic", "traditional", "perfect", "ultimate",
    "healthy", "quickest", "easiest", "warm", "cold", "hot", "fresh", "raw",
    "baked", "roasted", "grilled", "fried", "toasted", "steamed", "poached",
    "slow", "oven", "pan", "chargrilled", "griddled", "spiced", "spicy", "mini",
    "little", "big", "giant", "super", "creamy", "crispy", "crunchy", "nutty",
    "savory", "savoury", "sweet", "my", "our", "the", "a", "an", "of", "for",
    "style", "inspired", "very", "extra", "new", "old", "real", "proper",
    # Container nouns. They occupy the head position without naming a dish:
    # "Munchy muesli mix" is a muesli, and filing it under `mix` would put it
    # in a family with every other mix in the corpus while leaving it free to
    # appear beside an actual muesli.
    "mix", "bowl", "plate", "platter", "dish", "recipe", "medley", "selection",
    "pot", "tray", "bake", "pieces", "slices", "wedges",
})

# Irregular plurals the -s/-ies rules below get wrong. Small on purpose: only
# the ones that actually head a recipe title.
_IRREGULAR: dict[str, str] = {
    "leaves": "leaf", "loaves": "loaf", "halves": "half", "potatoes": "potato",
    "tomatoes": "tomato", "mangoes": "mango", "children": "child", "geese": "goose",
}


def _singular(word: str) -> str:
    """Collapse a plural head noun onto its singular.

    "Roasted balsamic thyme onions" and "Caramelised onion tart" are the same
    family, and a key that keeps the -s would file them apart.
    """
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3:-2] in ("s", "x", "z", "h"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def dish_family(title: str) -> str:
    """What kind of dish this is, as one word. Empty when unreadable.

    The head noun: the last real word of the title before it starts listing
    ingredients, singularised, with qualifiers stripped. "Gluten-free berry
    muesli" and "Toasted muesli" both give `muesli`; "Baked eggplant with
    cranberry and mint" gives `eggplant`, not `mint`.

    A blunt instrument, and knowingly so. It files "Roasted tomato and red
    lentil soup" under `soup` and "Sweet kumara with mushrooms" under `kumara`,
    which is right, but it has no idea that a frittata and an omelette are the
    same breakfast. It is meant to catch the four-mueslis case — repetition
    obvious enough that a member would name it — not to model cuisine.
    """
    head = _TAIL.split((title or "").lower(), maxsplit=1)[0]
    words = [w for w in re.findall(r"[a-z]+", head) if len(w) > 2]
    meaningful = [w for w in words if w not in _QUALIFIERS]
    # Everything was a qualifier ("Slow-roasted, oven-baked") — fall back to the
    # raw words rather than returning nothing, since an empty key would make
    # every such title collide into one family.
    chosen = meaningful or words
    return _singular(chosen[-1]) if chosen else ""


def select_diverse(
    rows: Sequence[dict[str, Any]],
    count: int,
    *,
    seen: dict[str, int] | None = None,
    max_per_family: int = 1,
    key: Callable[[dict[str, Any]], str] = lambda r: str(r.get("title") or ""),
) -> list[dict[str, Any]]:
    """Take `count` rows, preferring families not already used.

    Two passes rather than a filter. The first takes the best-ranked candidate
    of each unused family, in the order the search returned them — so ranking
    still decides *which* muesli, and this only decides that the second one
    waits. The second pass backfills from what was skipped, in the same order,
    when the corpus could not offer enough families.

    Backfilling matters more than it sounds: a strict rule would return four
    breakfasts where five were asked for, and a short plan is a worse answer
    than a repetitive one. Variety is a preference here, never a constraint.

    `seen` is shared across the whole plan and mutated, which is how day 7's
    breakfast knows about day 1's. Pass a fresh dict to scope it to one slot.
    """
    families = seen if seen is not None else {}
    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in rows:
        if len(picked) >= count:
            break
        family = dish_family(key(row))
        if family and families.get(family, 0) >= max_per_family:
            skipped.append(row)
            continue
        picked.append(row)
        if family:
            families[family] = families.get(family, 0) + 1

    for row in skipped:
        if len(picked) >= count:
            break
        picked.append(row)
        family = dish_family(key(row))
        if family:
            families[family] = families.get(family, 0) + 1

    return picked


def family_counts(titles: Iterable[str]) -> dict[str, int]:
    """How many of each family a set of titles contains. For tests and reports."""
    out: dict[str, int] = {}
    for title in titles:
        family = dish_family(title)
        if family:
            out[family] = out.get(family, 0) + 1
    return out
