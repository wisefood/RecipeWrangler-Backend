"""FoodChat meal-slot candidates, served from the catalog index.

The original implementation queries Neo4j, and its docstring explains why: "ES
has no dish-type tags so cannot pre-filter". That was true of `recipes_v2`. It
is not true of the catalog index, which carries canonical `course_types`
alongside the cuisine, mood, flavour and food-group annotations — and
`planning_tier`.

That gap had two consequences, neither visible from the planner:

**No annotations.** They exist only in Elasticsearch. A member whose profile
records a liking for Thai food could not have it honoured, because the store the
planner queried has never held a cuisine. FoodChat collects cuisine affinity and
a cooking-time preference and had nowhere to send either.

**No `planning_tier`.** The tier marks recipes unfit to serve unattended, and it
is applied only by `/api/v2/tools`. A recipe excluded from planning was excluded
from the tools endpoint and served normally by the endpoint the planner actually
uses — so the exclusion did nothing where it mattered.

The response contract is unchanged, deliberately and exactly: the same slot
keys, the same four item fields, `ingredients` and `directions` as flat strings
rather than lists. FoodChat's client reads exactly those (`candidates_client.py`
around line 191) and a shape change would break it silently, since it uses
`.get()` with defaults throughout.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from recipe_wrangler.catalog import diets as D
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog.nutrition import per_serving, within_targets

logger = logging.getLogger(__name__)

# Preferences dropped, in this order, when a slot cannot be filled.
#
# Same ladder as `/api/v2/tools/plan_meals`, and same reasoning: shed the
# weakest signal first. Mood is a mood; a cuisine is a stated taste; time is a
# practical limit someone will actually notice being violated.
RELAXATION_ORDER: tuple[str, ...] = (
    "moods",
    "flavor_profiles",
    "food_groups",
    "cuisines",
    "max_duration_minutes",
)

# Never relaxed, at any point, for any reason. An allergen exclusion that gets
# dropped to fill a slot is a safety failure, not a degraded result.
NEVER_RELAXED: frozenset[str] = frozenset(
    {"exclude_allergens", "exclude_ingredients", "diet"}
)

_SOURCE_FIELDS = [
    "recipe_id",
    "title",
    "ingredients",
    "instructions",
    "course_types",
    "duration",
    "source",
]


# Courses a main meal can be made of — the fallback for a slot nobody has
# mapped. Deliberately not empty: no filter means every course qualifies.
_MAIN_MEAL_COURSES: tuple[str, ...] = ("main-dish", "soup", "salad")

# How many extra candidates to request before macro filtering.
#
# `attach_nutrition` drops recipes that miss the member's calorie or protein
# targets, and it can only drop — so fetching exactly `wanted` returns a short
# or empty slot the moment a nutrition profile is set. The Neo4j path pools 5x
# for precisely this reason; matching it keeps the two paths comparable.
_MACRO_OVERSAMPLE = 5


# Ingredient words that indicate an allergen, beyond the allergen's own name.
#
# The declared `allergens` field is incomplete — a mozzarella recipe carries
# `allergens: []` and passed a milk exclusion. Substring matching alone does not
# close that: "dairy" appears in 27 recipes, while "cheese" appears in 1,328,
# and no amount of wildcarding turns one into the other.
#
# This is a name-level approximation of the taxonomy walk the Neo4j path does
# through FoodOn ancestry. It is deliberately generous — a false exclusion costs
# a recipe, a false inclusion costs a reaction — and deliberately not exhaustive:
# closing it properly needs the allergen→FoodOn-id map that does not exist yet,
# and pretending a word list is that map is how you get trusted for more than
# you are.
ALLERGEN_INGREDIENT_TERMS: dict[str, tuple[str, ...]] = {
    "milk": ("milk", "cheese", "cream", "butter", "yoghurt", "yogurt", "ghee",
             "custard", "mozzarella", "parmesan", "ricotta", "feta", "mascarpone"),
    "dairy": ("milk", "cheese", "cream", "butter", "yoghurt", "yogurt", "ghee",
              "custard", "mozzarella", "parmesan", "ricotta", "feta", "mascarpone"),
    "egg": ("egg", "mayonnaise", "meringue", "aioli"),
    "peanut": ("peanut", "groundnut", "satay"),
    "tree_nut": ("almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio",
                 "macadamia", "brazil nut", "praline", "marzipan", "pine nut",
                 "nut"),
    # A profile that just says "nuts" means all of them. This key was missing:
    # the lookup tried "nuts" and "nut", found neither, and fell back to a
    # bare `*nuts*` wildcard — which does not match "pine nut", singular, so
    # "Spinach with raisins and pine nuts" was served to a nut-allergic
    # member. The `nut` fragment is deliberately broad (it also catches
    # nutmeg and coconut); for an allergy, over-exclusion is the safe error.
    "nut": ("almond", "cashew", "walnut", "pecan", "hazelnut", "pistachio",
            "macadamia", "brazil nut", "praline", "marzipan", "pine nut",
            "peanut", "groundnut", "satay", "nut"),
    "wheat": ("wheat", "flour", "bread", "pasta", "couscous", "semolina",
              "breadcrumb", "pastry"),
    "gluten": ("wheat", "barley", "rye", "flour", "bread", "pasta", "couscous",
               "semolina", "breadcrumb", "pastry"),
    "soy": ("soy", "soya", "tofu", "edamame", "miso", "tempeh"),
    "fish": ("fish", "anchovy", "salmon", "tuna", "cod", "haddock", "sardine",
             "mackerel"),
    "shellfish": ("prawn", "shrimp", "crab", "lobster", "mussel", "oyster",
                  "clam", "scallop", "squid", "calamari"),
    "sesame": ("sesame", "tahini"),
}


def allergen_exclusions(allergen: str) -> list[dict[str, Any]]:
    """Clauses that, OR-ed under a `must_not`, exclude one allergen.

    Three sources, because no single one is complete:

    * the declared `allergens` field, when the graph recorded it;
    * an ingredient whose name *contains* the allergen word, via a wildcard on
      the keyword subfield. A plain analysed `match` cannot do this — "milk"
      does not match "buttermilk" — and the earlier version used one while
      claiming otherwise;
    * `ALLERGEN_INGREDIENT_TERMS`, which is what actually catches mozzarella
      for `dairy`. Substring matching alone cannot: the two words share no
      characters.

    Multi-word allergens also contribute each word separately, so "tree nuts"
    excludes an ingredient naming "nuts". Separate clauses rather than one
    analysed match, so they OR as exclusions instead of requiring all of them.
    """
    word = allergen.replace("_", " ").strip()
    if not word:
        return []

    clauses: list[dict[str, Any]] = [{"term": {"allergens": allergen}}]

    def contains(fragment: str) -> dict[str, Any]:
        return {
            "nested": {
                "path": "ingredients",
                "query": {
                    "wildcard": {
                        "ingredients.name.kw": {
                            "value": f"*{fragment}*",
                            "case_insensitive": True,
                        }
                    }
                },
            }
        }

    fragments = {word}
    fragments.update(part for part in word.split() if len(part) >= 3)
    # Both the underscored and spaced spellings key the table: callers normalise
    # to `tree_nut`, but a profile may say "tree nuts".
    for key in (allergen, allergen.rstrip("s"), word, word.replace(" ", "_")):
        fragments.update(ALLERGEN_INGREDIENT_TERMS.get(key, ()))

    for fragment in sorted(fragments):
        clauses.append(contains(fragment))
    return clauses


# Backwards-compatible name for callers inside this module's history.
_allergen_exclusions = allergen_exclusions


def _flatten_ingredients(value: Any) -> str:
    """The nested ingredient array, back to the comma string FoodChat expects.

    Its client does `str(r.get("ingredients", ""))` — handed a list it would
    stringify the Python repr, brackets and quotes included, and feed that to a
    language model as the recipe's ingredients.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)):
        return ""
    names = [
        (item.get("name") if isinstance(item, dict) else item) for item in value
    ]
    return ", ".join(str(name).strip() for name in names if name)


def _flatten_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(part).strip() for part in value if part)
    return str(value or "")


def _norm(values: Iterable[str]) -> list[str]:
    """Lowercase, underscore-separate — the index's spelling.

    `terms` does not analyse its input, so "middle eastern" matches nothing
    against a field storing `middle_eastern`. Silently, with a 200.
    """
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text and text not in out:
            out.append(text)
    return out


def _hard_filters(request: Any, course_types: list[str]) -> list[dict[str, Any]]:
    """Constraints that are never relaxed, plus the slot's course."""
    constraints = request.constraints
    profile = request.user_profile

    filters: list[dict[str, Any]] = [
        {"term": {"has_profile": True}},
        # Withdrawn recipes are never planned. The Neo4j path this replaces
        # applies `NEO4J_NOT_DISABLED`; omitting it here would have made the new
        # path *less* safe than the old one, serving a recipe someone had
        # deliberately taken out of circulation into an automated meal plan.
        {"bool": {"must_not": [{"terms": {"status": ["disabled", "deleted", "archived"]}}]}},
        # Planning eligibility, honoured here for the first time. `excluded` is
        # a decision someone made about a recipe's fitness to be served without
        # review; serving it in an automated plan is exactly what it forbids.
        {"bool": {"must_not": [{"term": {"planning_tier": "excluded"}}]}},
    ]

    if course_types:
        filters.append({"terms": {"course_types": course_types}})

    # Allergen exclusion, deliberately broader than the `allergens` field alone.
    #
    # That field is derived from HAS_ALLERGEN edges in the graph, and it is
    # incomplete: a mozzarella recipe with `allergens: []` passed a milk
    # exclusion. The Neo4j path does not have this problem because it also walks
    # ingredient names and FoodOn ancestry, so matching only the flat field here
    # would make the new path *less* safe than the one it replaces — the one
    # regression that is not acceptable to trade for better recommendations.
    #
    # Two clauses per allergen, OR-ed as an exclusion: the declared allergen,
    # and any ingredient naming it. The second catches "mozzarella cheese" for
    # `dairy` and "buttermilk" for `milk` where the edge is missing.
    #
    # Still narrower than Neo4j's taxonomic walk, which resolves ancestor
    # *names* — Elasticsearch stores FoodOn ancestry as ids, so an equivalent
    # check needs the id set for each allergen, not a name match. Until that
    # exists, FOODCHAT_CANDIDATES_FROM_ELASTIC should stay off in production.
    for allergen in _norm(getattr(profile, "allergies", []) or []):
        filters.append(
            {"bool": {"must_not": _allergen_exclusions(allergen)}}
        )

    for ingredient in (getattr(constraints, "exclude_ingredients", []) or []):
        text = str(ingredient or "").strip().lower()
        if text:
            filters.append(
                {
                    "bool": {
                        "must_not": [
                            {
                                "nested": {
                                    "path": "ingredients",
                                    "query": {
                                        "match_phrase": {"ingredients.name": text}
                                    },
                                }
                            }
                        ]
                    }
                }
            )

    diet = _norm(getattr(profile, "diet", []) or [])

    # Claiming the diet is not enough. 44% of this corpus's `vegan` recipes
    # contain meat, dairy, egg or honey by ingredient name — the tags come from
    # the sources and were never checked against the ingredient lists. A member
    # who asks for vegan and is served scrambled eggs has been actively misled.
    filters.extend(D.exclusion_filters(diet))

    for tag in diet:
        # Same three-way match the search path uses. `suitable_for` is the
        # better signal but is unpopulated until the vegan/vegetarian classifier
        # runs, so falling back to the source tags is what keeps diet filtering
        # working at all today.
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"suitable_for": tag}},
                        {"term": {"diet_tags": tag}},
                        {"term": {"tags": tag}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    return filters


def _soft_filters(request: Any, dropped: set[str]) -> list[dict[str, Any]]:
    """The preferences still in force after `dropped` have been shed."""
    constraints = request.constraints
    filters: list[dict[str, Any]] = []

    for facet in ("cuisines", "moods", "flavor_profiles", "food_groups"):
        if facet in dropped:
            continue
        values = _norm(getattr(constraints, facet, []) or [])
        if values:
            filters.append({"terms": {facet: values}})

    if "max_duration_minutes" not in dropped:
        limit = getattr(constraints, "max_duration_minutes", None)
        if limit:
            filters.append({"range": {"duration": {"lte": int(limit)}}})

    return filters


def _include_ingredient_clauses(request: Any) -> list[dict[str, Any]]:
    """Wanted ingredients — a boost, not a filter.

    The Neo4j implementation ranks by how many are present rather than
    requiring them, and a meal plan that returns nothing because the member
    likes chickpeas would be a worse plan, not a stricter one.
    """
    clauses = []
    for ingredient in (request.constraints.include_ingredients or []):
        text = str(ingredient or "").strip().lower()
        if text:
            clauses.append(
                {
                    "nested": {
                        "path": "ingredients",
                        "query": {"match_phrase": {"ingredients.name": text}},
                        "score_mode": "max",
                    }
                }
            )
    return clauses


def _slot_course_types(slot: str) -> list[str]:
    """Canonical course types for a meal slot.

    Goes through the same canonicaliser the rest of the catalog uses, so a slot
    named `lunch` resolves to `main-dish` rather than matching nothing — the
    index folded lunch and dinner into main-dish, and a planner asking for
    literal "lunch" would get an empty slot.
    """
    canonical = S.canonical_course_type(slot)
    if canonical:
        return [canonical]
    # An unrecognised slot must not become "no course filter at all" — that
    # fills `supper` with desserts and beverages, which is worse than an empty
    # slot because it looks like a working plan. Fall back to the courses a
    # main meal is made of.
    logger.info("unknown meal slot %r — planning it as a main meal", slot)
    return list(_MAIN_MEAL_COURSES)


def fetch_candidates_es(request: Any) -> dict[str, list[dict[str, Any]]]:
    """Candidates per meal slot, from the catalog index.

    Drop-in for `fetch_foodchat_candidates`: same argument, same return shape.
    Nutrition is attached by the caller, exactly as before.
    """
    from recipe_wrangler.catalog.entities import recipe_entity

    entity = recipe_entity()
    quotas: dict[str, int] = dict(request.quotas or {})
    exclude_ids = [
        str(rid).strip()
        for rid in (request.constraints.exclude_recipe_ids or [])
        if str(rid).strip()
    ]
    favorites = set(
        str(rid).strip()
        for rid in (getattr(request.constraints, "favorite_recipe_ids", []) or [])
        if str(rid).strip()
    )

    results: dict[str, list[dict[str, Any]]] = {}
    used: list[str] = list(exclude_ids)

    for slot, quota in quotas.items():
        wanted = max(0, int(quota or 0))
        if not wanted:
            results[slot] = []
            continue

        course_types = _slot_course_types(slot)
        hard = _hard_filters(request, course_types)
        boosts = _include_ingredient_clauses(request)
        dropped: set[str] = set()
        hits: list[dict[str, Any]] = []

        for step in range(len(RELAXATION_ORDER) + 1):
            filters = [*hard, *_soft_filters(request, dropped)]
            if used:
                filters.append(
                    {"bool": {"must_not": [{"terms": {"recipe_id": used}}]}}
                )

            # Over-fetch: `attach_nutrition` filters by macro targets after the
            # search and can only remove, so asking for exactly `wanted` returns
            # a short slot whenever a nutrition profile is set.
            oversample = (
                wanted * _MACRO_OVERSAMPLE
                if getattr(request.constraints, "nutrition_profile", None)
                else wanted
            )
            body: dict[str, Any] = {
                "size": oversample,
                "_source": _SOURCE_FIELDS,
                "query": {
                    "bool": {
                        "filter": filters,
                        "should": [
                            *boosts,
                            # Favourites float up; they are never filtered in,
                            # so diet and allergen rules still apply to them.
                            *(
                                [{"terms": {"recipe_id": sorted(favorites), "boost": 5}}]
                                if favorites
                                else []
                            ),
                        ],
                    }
                },
                # `planning_tier` first so `preferred` recipes lead, then score,
                # then recipe_id as a total tiebreak — without a total order
                # Elasticsearch falls back to internal doc order and the same
                # request can return a different plan each time.
                "sort": [
                    {"planning_tier": {"order": "asc"}},
                    "_score",
                    {"recipe_id": "asc"},
                ],
            }
            if request.randomize:
                # `sum`, not `replace` and not `multiply`.
                #
                # `replace` discarded the favourites boost, the liked-ingredient
                # boosts and — because the sort collapsed to `_score` alone —
                # the `planning_tier` ordering, so a randomised request ignored
                # every preference it had just been given.
                #
                # `multiply` looks like the careful fix and is worse: a
                # candidate query is almost entirely `filter` clauses, which do
                # not score, so `_score` is 0 and 0 x random is 0. Every recipe
                # ties, the tiebreak is `recipe_id`, and "randomize" returns the
                # same three recipes every single time — which is exactly what
                # it did.
                #
                # `sum` adds the random component to whatever the boosts
                # produced: it shuffles ties, and a real boost still outranks
                # the noise. `planning_tier` stays the primary sort key, so
                # `preferred` recipes lead and the shuffle happens inside a tier.
                body["query"] = {
                    "function_score": {
                        "query": body["query"],
                        "random_score": {},
                        "boost_mode": "sum",
                    }
                }
                body["sort"] = [
                    {"planning_tier": {"order": "asc"}},
                    "_score",
                    {"recipe_id": "asc"},
                ]

            response = entity.es.search_body(entity.alias, body)
            hits = response.get("hits", {}).get("hits", [])
            if len(hits) >= wanted or step >= len(RELAXATION_ORDER):
                break

            # Shed the next preference and try again. Recorded at INFO because
            # a plan that quietly ignored a stated preference is indistinguishable
            # from one that never received it.
            facet = RELAXATION_ORDER[step]
            if getattr(request.constraints, facet, None):
                logger.info(
                    "foodchat slot %r: dropping %s (only %d of %d matched)",
                    slot,
                    facet,
                    len(hits),
                    wanted,
                )
            dropped.add(facet)

        items: list[dict[str, Any]] = []
        for hit in hits:
            src = hit.get("_source") or {}
            recipe_id = str(src.get("recipe_id") or "").strip()
            if not recipe_id:
                continue
            items.append(
                {
                    "recipe_id": recipe_id,
                    "title": str(src.get("title") or "").strip(),
                    "ingredients": _flatten_ingredients(src.get("ingredients")),
                    "directions": _flatten_text(src.get("instructions")),
                    "dish_type": slot,
                }
            )
            used.append(recipe_id)

        results[slot] = items
        quotas[slot] = wanted

    results = attach_nutrition(results, request.constraints.nutrition_profile)

    # Back down to what was asked for. The over-fetch above exists so macro
    # filtering has something to discard; without this the caller receives the
    # surplus — a request for 5 breakfasts came back with 13.
    return {slot: items[: quotas.get(slot, len(items))] for slot, items in results.items()}


# --------------------------------------------------------------------------- #
# Nutrition — attached from Postgres, and used to enforce macro targets.
#
# Elasticsearch carries a Nutri-Score but not the per-serving macros the meal
# planner filters on, so this stays a Postgres read whichever store supplied
# the candidates. Kept module-level rather than nested so both the shaping and
# the threshold check can be tested directly; the Neo4j implementation still
# carries its own copy of this logic, which is the next thing to converge.
# --------------------------------------------------------------------------- #

def attach_nutrition(
    results: dict[str, list[dict[str, Any]]], nutrition_profile: Any = None
) -> dict[str, list[dict[str, Any]]]:
    """Add `nutrition` to every candidate, dropping those outside the targets.

    One batched Postgres read across all slots rather than one per recipe.
    """
    from recipe_wrangler.repositories.postgres_nutrition import (
        get_recipe_nutrition_batch,
    )

    ids = [c["recipe_id"] for items in results.values() for c in items]
    if not ids:
        return results

    try:
        raw_by_id = get_recipe_nutrition_batch(ids) or {}
    except Exception as exc:  # noqa: BLE001
        # Candidates without macros are still usable candidates. Failing the
        # whole request would turn a nutrition outage into no meal plan at all.
        logger.warning("nutrition batch failed for %d ids: %s", len(ids), exc)
        raw_by_id = {}

    for slot, items in results.items():
        kept = []
        for item in items:
            macros = per_serving(raw_by_id.get(item["recipe_id"]))
            if not within_targets(macros, nutrition_profile):
                continue
            item["nutrition"] = macros
            kept.append(item)
        results[slot] = kept
    return results
