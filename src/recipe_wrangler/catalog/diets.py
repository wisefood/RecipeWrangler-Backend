"""Diet verification — because the corpus's diet tags are wrong at scale.

A recipe titled "Tomato and avocado scrambled eggs" is tagged `vegan` in this
corpus. It is not an outlier: **44% of the 2,143 recipes tagged `vegan` contain
meat, dairy, egg or honey by ingredient name**, and 11% of those tagged
`vegetarian` contain meat or fish.

The tags come from the upstream sources and were never verified against the
ingredient lists. Filtering on them alone is worse than having no diet filter:
a member who asks for vegan and is served eggs has been actively misled, and
has less reason to check than one who was told nothing.

So a diet filter is two conditions, not one:

1. the recipe claims the diet — the existing tag match; and
2. no ingredient contradicts it — this module.

This is the same shape as the allergen backstop in `catalog.foodchat`, for the
same reason and with the same limits. It is a **name-level** check: it cannot
see that "Worcestershire sauce" contains anchovy, and it will exclude a recipe
whose "beef tomato" is a tomato. Both error toward exclusion, which is the
correct direction for a constraint someone stated about their own eating.

The real fix is `scripts/classify_vegan_vegetarian.py` populating
`consumer_suitability` from the ingredient graph — three-state, with blocking
ingredients and reason codes, which is what `suitable_for` is for. It has never
been run against this corpus (0 recipes carry it), and until it has, this is
what stands between a stated diet and a plate of eggs.
"""

from __future__ import annotations

from typing import Any, Iterable

# Ingredient-name fragments that contradict a diet.
#
# Matched as substrings against `ingredients.name`, case-insensitively, so
# "egg" catches "egg white" and "free-range eggs". That breadth is deliberate;
# see the module docstring on which direction to err in.
DIET_EXCLUDED_INGREDIENTS: dict[str, tuple[str, ...]] = {
    "vegan": (
        # meat and fish
        "chicken", "beef", "pork", "lamb", "veal", "bacon", "ham", "sausage",
        "salami", "prosciutto", "chorizo", "turkey", "duck", "venison",
        # Cuts and preparations that never say the animal. "Bacon and sweetcorn
        # baked potato" lists its bacon as "rashers"; "Beef with balsamic
        # dressing" lists "sirloin steak". A word list built from animal names
        # alone passed both.
        "rasher", "steak", "mince", "brisket", "sirloin", "rump",
        "cutlet", "meatball", "pancetta", "pepperoni", "pastrami",
        "liver", "kidney", "poultry", "gammon", "burger patty",
        "fish", "salmon", "tuna", "cod", "haddock", "anchovy", "sardine",
        "mackerel", "prawn", "shrimp", "crab", "lobster", "mussel", "oyster",
        "clam", "scallop", "squid", "calamari",
        # dairy and eggs
        "egg", "milk", "cheese", "cream", "butter", "yoghurt", "yogurt",
        "ghee", "custard", "mozzarella", "parmesan", "ricotta", "feta",
        "mascarpone", "halloumi", "paneer",
        # other animal products
        "honey", "gelatine", "gelatin", "lard", "suet", "worcestershire",
    ),
    "vegetarian": (
        "chicken", "beef", "pork", "lamb", "veal", "bacon", "ham", "sausage",
        "salami", "prosciutto", "chorizo", "turkey", "duck", "venison",
        # Cuts and preparations that never say the animal. "Bacon and sweetcorn
        # baked potato" lists its bacon as "rashers"; "Beef with balsamic
        # dressing" lists "sirloin steak". A word list built from animal names
        # alone passed both.
        "rasher", "steak", "mince", "brisket", "sirloin", "rump",
        "cutlet", "meatball", "pancetta", "pepperoni", "pastrami",
        "liver", "kidney", "poultry", "gammon", "burger patty",
        "fish", "salmon", "tuna", "cod", "haddock", "anchovy", "sardine",
        "mackerel", "prawn", "shrimp", "crab", "lobster", "mussel", "oyster",
        "clam", "scallop", "squid", "calamari",
        "gelatine", "gelatin", "lard", "suet", "worcestershire",
    ),
    "pescatarian": (
        "chicken", "beef", "pork", "lamb", "veal", "bacon", "ham", "sausage",
        "salami", "prosciutto", "chorizo", "turkey", "duck", "venison",
        # Cuts and preparations that never say the animal. "Bacon and sweetcorn
        # baked potato" lists its bacon as "rashers"; "Beef with balsamic
        # dressing" lists "sirloin steak". A word list built from animal names
        # alone passed both.
        "rasher", "steak", "mince", "brisket", "sirloin", "rump",
        "cutlet", "meatball", "pancetta", "pepperoni", "pastrami",
        "liver", "kidney", "poultry", "gammon", "burger patty",
        "lard", "suet",
    ),
    "dairy_free": (
        "milk", "cheese", "cream", "butter", "yoghurt", "yogurt", "ghee",
        "custard", "mozzarella", "parmesan", "ricotta", "feta", "mascarpone",
        "halloumi", "paneer",
    ),
    "nut_free": (
        "almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio",
        "macadamia", "praline", "marzipan", "peanut",
    ),
    "gluten_free": (
        "wheat", "barley", "rye", "flour", "breadcrumb", "semolina",
        "couscous", "pastry",
    ),
}

# Aliases, so a caller's spelling does not decide whether the check runs.
_ALIASES: dict[str, str] = {
    "dairyfree": "dairy_free",
    "dairy-free": "dairy_free",
    "nutfree": "nut_free",
    "nut-free": "nut_free",
    "glutenfree": "gluten_free",
    "gluten-free": "gluten_free",
    "vegetarian_or_vegan": "vegetarian",
    "pescatarian_safe": "pescatarian",
    "vegans": "vegan",
    "vegetarians": "vegetarian",
}


def canonical_diet(tag: object) -> str | None:
    """The diet name this module knows, or None if it verifies nothing.

    An unknown diet returns None rather than raising: `low_carb` is a real tag
    with no ingredient contradiction, and refusing to filter on it would be
    worse than not verifying it.
    """
    name = str(tag or "").strip().lower().replace(" ", "_")
    name = _ALIASES.get(name, name)
    return name if name in DIET_EXCLUDED_INGREDIENTS else None


def excluded_ingredients(diet: object) -> tuple[str, ...]:
    """Ingredient fragments that contradict this diet. Empty if unverifiable."""
    name = canonical_diet(diet)
    return DIET_EXCLUDED_INGREDIENTS.get(name, ()) if name else ()


def contradiction(text: str, diets: Iterable[str]) -> tuple[str, str] | None:
    """The first `(diet, ingredient_fragment)` a text contradicts, or None.

    A client-side counterpart to the query filter below — the same check, run
    on what actually came back. Both exist for the reason the allergen backstop
    does: one filter between a member and a plate of eggs is one too few, and
    this corpus has already proved it needs the second.
    """
    haystack = (text or "").lower()  # caller passes title + ingredients
    for diet in diets or ():
        for fragment in excluded_ingredients(diet):
            if fragment in haystack:
                return (str(diet), fragment)
    return None


def token_variants(fragment: str) -> tuple[str, ...]:
    """A fragment plus the other number-forms the index stores as new words.

    `default_text` does not stem. `eggs` is not `egg` to it, so a phrase query
    for `egg` matched 1,368 recipes and let **131 others through** — "Poached
    eggs" was served to a member who had asked for vegan, which is the exact
    failure this module exists to prevent.

    **Both directions.** The first version only pluralised, on the assumption
    that word lists are written in the singular — and then a member excluded
    "mushrooms" and was served "Mushroom and silver beet vege scramble",
    whose every mushroom is singular. What a *member* types is not a curated
    list; it arrives in whichever number they thought in.

    Rules enough for ingredient nouns, not a general inflector: only the last
    word changes, so "burger patty" becomes "burger patties". False forms
    ("creams", "lambs") cost nothing — a form the corpus does not use simply
    matches nothing.

    The proper fix is a stemmed subfield on `title` and `ingredients.name`,
    which needs a mapping change and a reindex. Until then this closes the gap
    without one.
    """
    word = (fragment or "").strip().lower()
    if not word:
        return ()
    head, _, last = word.rpartition(" ")

    def _form(last_word: str) -> str:
        return f"{head} {last_word}".strip()

    variants = [word]

    # plural of the given form
    if last.endswith(("s", "x", "z", "ch", "sh")):
        variants.append(_form(last + "es"))
    elif last.endswith("y") and last[-2:-1] not in "aeiou":
        variants.append(_form(last[:-1] + "ies"))
    elif last.endswith("f"):
        variants.append(_form(last[:-1] + "ves"))
    else:
        variants.append(_form(last + "s"))

    # singular of the given form, when it looks plural
    if last.endswith("ies") and len(last) > 4:
        variants.append(_form(last[:-3] + "y"))
    elif last.endswith("ves") and len(last) > 4:
        variants.append(_form(last[:-3] + "f"))
    elif last.endswith("es") and len(last) > 3 and last[-3] in "sxz":
        variants.append(_form(last[:-2]))
    elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
        variants.append(_form(last[:-1]))

    return tuple(dict.fromkeys(v for v in variants if v))


def exclusion_filters(diets: Iterable[str]) -> list[dict[str, Any]]:
    """Elasticsearch clauses excluding recipes that contradict these diets.

    One `must_not` per diet, each holding a nested wildcard per fragment. Kept
    as separate clauses rather than one giant `should` so a query plan can be
    read, and so adding a diet does not silently widen an existing one.
    """
    out: list[dict[str, Any]] = []
    for diet in diets or ():
        fragments = excluded_ingredients(diet)
        if not fragments:
            continue

        clauses: list[dict[str, Any]] = []
        # Singular and plural both: the index does not stem, so they are two
        # different words to it. See `token_variants`.
        for fragment in dict.fromkeys(v for f in fragments for v in token_variants(f)):
            # `match_phrase` on the analysed field, not a wildcard on the
            # keyword one. A substring match is catastrophic here: `*chop*`
            # matched 227 recipes because "chopped" appears in nearly every
            # ingredient list, and `*egg*` excluded all 176 eggplant recipes
            # from vegan. Token matching takes `chop` to 26 and eggplant to 16.
            #
            # Over-exclusion is the failure mode to fear. It is silent — the
            # corpus just looks smaller — and it makes the filter untrustworthy
            # in the opposite direction from the bug it was built to fix.
            clauses.append(
                {
                    "nested": {
                        "path": "ingredients",
                        "query": {"match_phrase": {"ingredients.name": fragment}},
                    }
                }
            )
            # The title as well. "Bacon and sweetcorn baked potato" lists its
            # bacon as "rashers" and "Beef with balsamic dressing" lists
            # "sirloin steak" — an ingredient-only check passed both while the
            # dish announced itself in its own name. A title is a claim about
            # what a recipe is, and it is worth reading.
            clauses.append({"match_phrase": {"title": fragment}})

        out.append({"bool": {"must_not": clauses}})
    return out
