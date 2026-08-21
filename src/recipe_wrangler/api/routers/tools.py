"""Agent-facing tool surface for FoodChat and other LLM callers.

Why this exists separately from ``/api/v2/recipes``: an agent needs a *narrow,
self-describing* surface, not a query language. ``/api/v2/recipes/search``
expects the caller to know Lucene syntax and the field names; an LLM should be
handed a small set of named capabilities with enumerated options and be told
what happens when a request cannot be satisfied.

Three ideas shape the design:

**Closed vocabularies.** Every option is enumerated at ``GET /api/v2/tools``.
An agent that invents ``cuisine="fusion"`` gets it rejected and reported, not
silently dropped — the caller can then tell the user "we don't classify that"
rather than returning an unexplained empty list.

**Relaxation instead of empty results.** A meal plan for "Thai, vegan, under 20
minutes, nut-free" may have no answer. Returning ``[]`` is useless to an agent
mid-conversation. Instead constraints are dropped in a fixed order — softest
first, never a safety constraint — and every drop is reported in
``relaxations`` so the agent can say *"no Thai options, so I widened to
Southeast Asian"*.

**Allergens are never relaxed.** Everything else is a preference; an allergen
exclusion is a safety boundary. It stays in the query at every relaxation
level, and if the result is empty the answer is genuinely empty.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.identity import Caller, get_caller, redact
from recipe_wrangler.catalog import diets as D
from recipe_wrangler.catalog import foodchat as F
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog import variety
from recipe_wrangler.catalog import vocabularies as V
from recipe_wrangler.catalog.entities import recipe_entity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/tools", tags=["agent tools"])

# Which course types are plausible for a named meal slot. Slots are times of
# day; course types are what the dish *is*. Keeping the mapping here rather
# than in the caller means an agent can ask for "lunch" without knowing the
# corpus files most lunches as `main-dish`.
SLOT_COURSE_TYPES: dict[str, tuple[str, ...]] = {
    "breakfast": ("breakfast",),
    "brunch": ("breakfast", "main-dish", "salad"),
    "lunch": ("main-dish", "salad", "soup", "starter"),
    "dinner": ("main-dish", "soup", "salad"),
    "snack": ("snacks",),
    "dessert": ("desserts",),
    "drink": ("beverages",),
    "side": ("side",),
}

# Order in which preferences are surrendered when a query is too narrow.
# Cuisine goes first because it is the most arbitrary; duration last because a
# user who said "under 20 minutes" usually means it. Allergens and diet are
# absent by design — see the module docstring.
# Preferences surrendered, in order, when a slot cannot be filled.
#
# `food_groups` belongs here and was missing: it was applied unconditionally and
# never dropped, which made it a hard filter in behaviour while the manifest
# listed only allergens, ingredients and diet as `never_relaxed`. A member
# asking for fish would get an empty dinner rather than a relaxed one, and the
# service's own description of itself was wrong.
#
# It relaxes after flavour and before cuisine: a stated food group is a firmer
# signal than a taste but weaker than a named cuisine. Matches the ladder in
# `catalog.foodchat`, so the two planning paths degrade the same way.
# `tags` leads the ladder because a claim is the softest thing a caller can ask
# for and the scarcest thing to satisfy: `high_fibre` is on 457 of 4500 recipes,
# so two claims ANDed across 21 slots would starve most of them. Dropping it
# first means "high protein and high fibre" narrows the search when it can and
# widens when it cannot, instead of returning an empty week.
RELAXATION_ORDER: tuple[str, ...] = (
    "tags", "moods", "flavor_profiles", "food_groups", "cuisines", "max_minutes",
)

# Slots where the member expects to eat a meal, not a component of one.
# `variety.is_meal` screens these; a request for slot `side` or `dessert` is a
# request for exactly that and is left alone.
MEAL_SLOTS: frozenset[str] = frozenset({"breakfast", "brunch", "lunch", "dinner"})


class MealSlotRequest(BaseModel):
    # Unknown fields are a 422, not a shrug. This surface exists for LLM
    # agents, and Pydantic's default of silently discarding unrecognised
    # fields turns a misremembered name into an unfiltered query: a caller
    # who sent `query` instead of `q` got the whole corpus back, ranked by
    # nothing, with no hint anything was wrong. The module's own rule —
    # rejected and reported, never silently dropped — applies to field
    # names as much as to vocabulary values.
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(..., description="breakfast | brunch | lunch | dinner | snack | dessert | drink | side")
    count: int = Field(3, ge=1, le=20, description="How many candidates to return for this slot.")
    course_types: list[str] = Field(
        default_factory=list,
        description="Override the default course types for this slot.",
    )


class MealPlanRequest(BaseModel):
    """A meal-planning request. Every field is optional except ``slots``."""

    # See MealSlotRequest: unknown fields are rejected, not ignored.
    model_config = ConfigDict(extra="forbid")

    slots: list[MealSlotRequest] = Field(
        ..., description="The meal slots to fill. Order is preserved in the response."
    )
    days: int = Field(1, ge=1, le=14, description="Plan this many days; candidates are not reused across days.")

    cuisines: list[str] = Field(default_factory=list, description="Preferred cuisines. See /api/v2/tools for the list.")
    moods: list[str] = Field(default_factory=list, description="Preferred eating occasions, e.g. comfort, light, quick.")
    flavor_profiles: list[str] = Field(default_factory=list, description="Preferred tastes, e.g. spicy, umami.")
    food_groups: list[str] = Field(default_factory=list, description="Food groups that must be present.")
    tags: list[str] = Field(
        default_factory=list,
        description="Claim tags to prefer, e.g. high_protein, high_fibre, "
                    "low_calorie, 30_minutes_or_less. See /api/v2/tools for the "
                    "planning-relevant list. The FIRST preference surrendered "
                    "when a slot cannot be filled, because claims are the "
                    "scarcest annotation in the corpus.",
    )

    diet: list[str] = Field(default_factory=list, description="Dietary requirements, e.g. vegetarian, gluten_free. Never relaxed.")
    exclude_allergens: list[str] = Field(default_factory=list, description="Allergens to exclude. NEVER relaxed.")
    exclude_ingredients: list[str] = Field(default_factory=list, description="Ingredients to exclude. Never relaxed.")
    include_ingredients: list[str] = Field(default_factory=list, description="Ingredients that must appear.")

    max_minutes: int | None = Field(None, ge=1, description="Maximum total time.")
    min_nutri_score: Literal["A", "B", "C", "D", "E"] | None = Field(
        None, description="Worst acceptable Nutri-Score, e.g. 'B' allows A and B."
    )
    sources: list[str] = Field(default_factory=list, description="Restrict to these recipe collections.")
    exclude_recipe_ids: list[str] = Field(default_factory=list, description="Never return these — e.g. already planned.")
    favorite_recipe_ids: list[str] = Field(
        default_factory=list,
        description="Recipes the member has favourited. A ranking boost only — "
                    "they float to the top of their slot but every hard filter "
                    "still applies, so a favourite that breaks the diet or "
                    "carries an excluded allergen is not returned.",
    )

    allow_relaxation: bool = Field(True, description="Drop soft preferences rather than return nothing.")


NUTRI_RANK = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}


def _validate_options(payload: MealPlanRequest) -> tuple[dict[str, list[str]], list[str]]:
    """Split requested options into recognised values and rejected ones.

    An unrecognised value is reported rather than silently ignored: an agent
    that asked for a cuisine we do not classify should be able to say so.
    """
    accepted: dict[str, list[str]] = {}
    rejected: list[str] = []

    for facet, requested in (
        ("cuisines", payload.cuisines),
        ("moods", payload.moods),
        ("flavor_profiles", payload.flavor_profiles),
        ("food_groups", payload.food_groups),
    ):
        valid = V.validate_values(facet, requested)
        accepted[facet] = valid
        rejected += [
            f"{facet}={value}"
            for value in requested
            if str(value).strip().lower().replace(" ", "_") not in valid
        ]

    # `tags` is a human-authored OPEN field, so an unlisted value is not
    # necessarily wrong — it may simply be newer than PLANNING_TAGS. It is
    # reported (so a caller can see it was unusual) and still applied, because
    # it relaxes first and therefore cannot strand a slot.
    requested_tags: list[str] = []
    for value in payload.tags:
        slug = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        if not slug or slug in requested_tags:
            continue
        requested_tags.append(slug)
        if slug not in V.PLANNING_TAGS:
            rejected.append(f"tags={value}")
    accepted["tags"] = requested_tags

    resolved_sources: list[str] = []
    for name in payload.sources:
        source = S.resolve(name)
        if source is None:
            rejected.append(f"sources={name}")
        else:
            resolved_sources.append(source.raw)
    accepted["sources"] = resolved_sources
    return accepted, rejected


def _filters(
    payload: MealPlanRequest,
    accepted: dict[str, list[str]],
    course_types: tuple[str, ...],
    dropped: set[str],
) -> list[dict[str, Any]]:
    """Build the Elasticsearch filter list for one slot at one relaxation level."""
    filters: list[dict[str, Any]] = []

    if course_types:
        filters.append({"terms": {"course_types": list(course_types)}})

    # Planning eligibility. A recipe can be perfectly findable by a person and
    # still be a poor thing to put in someone's week unattended — missing
    # nutrition, an unresolved ingredient, a novelty item. `status` cannot
    # express that: hiding the recipe is too blunt. Agent surfaces therefore
    # exclude `planning_tier: excluded` while ordinary search still returns it.
    filters.append(
        {"bool": {"must_not": [{"term": {"planning_tier": "excluded"}}]}}
    )

    # --- never relaxed --------------------------------------------------- #
    for allergen in payload.exclude_allergens:
        # The full backstop, not just the annotation. A `term` on `allergens`
        # trusts the graph to have recorded every allergen, and it has not:
        # "Spinach with raisins and pine nuts" carries no annotation and went
        # to a nut-allergic member. `allergen_exclusions` adds name-level
        # ingredient matching — the same three-source check the candidates
        # path has always used. One filter between a member and an allergen
        # is one too few.
        filters.append({"bool": {"must_not": F.allergen_exclusions(allergen)}})
    for ingredient in payload.exclude_ingredients:
        # Singular and plural, ingredients and title — the same treatment the
        # diet filter needed, for the same analyzer. "Sweet kumara with
        # mushrooms" reached a member who excluded "mushrooms": its ingredient
        # row says "swiss brown mushroom", singular, a different token to an
        # index that does not stem. The title check catches recipes that
        # announce the ingredient without itemising it.
        clauses: list[dict[str, Any]] = []
        for variant in D.token_variants(ingredient):
            clauses.append(
                {
                    "nested": {
                        "path": "ingredients",
                        "query": {"match_phrase": {"ingredients.name": variant}},
                    }
                }
            )
            clauses.append({"match_phrase": {"title": variant}})
        if clauses:
            filters.append({"bool": {"must_not": clauses}})
    for tag in payload.diet:
        slug = "_".join(str(tag).strip().lower().replace("-", " ").split())
        if slug in {"vegan", "vegetarian"}:
            # Match the evidence-based assessment OR the source tag.
            #
            # `suitable_for` is the better answer — it comes from the FATO
            # three-state consumer-group assessment with blocking ingredients
            # and reason codes. But the classifier that populates it has never
            # been run against this corpus: there are zero ConsumerGroup nodes
            # in the graph, so the field is empty on all 7,221 recipes while
            # `diet_tags` carries vegetarian on 3,916 and vegan on 2,143.
            #
            # Filtering on `suitable_for` alone therefore returns nothing —
            # which is exactly the bug that makes /api/v1 vegetarian search
            # come back empty today. A `should` over both uses the evidence
            # when it exists and degrades to the tag when it does not, and
            # needs no change once the classifier runs.
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"suitable_for": slug}},
                            {"term": {"diet_tags": slug}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        else:
            filters.append({"term": {"diet_tags": slug}})

    # Verify the claim against the ingredients. See `catalog.diets`: the tag
    # alone admits eggs into a vegan plan, at scale.
    filters.extend(D.exclusion_filters(payload.diet))
    for ingredient in payload.include_ingredients:
        filters.append(
            {
                "nested": {
                    "path": "ingredients",
                    "query": {"match_phrase": {"ingredients.name": ingredient}},
                }
            }
        )
    if payload.min_nutri_score:
        filters.append(
            {"range": {"default_nutri_rank": {"lte": NUTRI_RANK[payload.min_nutri_score]}}}
        )
    if accepted["sources"]:
        filters.append({"terms": {"source": accepted["sources"]}})
    if payload.exclude_recipe_ids:
        filters.append(
            {"bool": {"must_not": [{"terms": {"recipe_id": payload.exclude_recipe_ids}}]}}
        )

    # --- relaxable ------------------------------------------------------- #
    if "tags" not in dropped and accepted.get("tags"):
        filters.append({"terms": {"tags": accepted["tags"]}})
    if "cuisines" not in dropped and accepted["cuisines"]:
        filters.append({"terms": {"cuisines": accepted["cuisines"]}})
    if "moods" not in dropped and accepted["moods"]:
        filters.append({"terms": {"moods": accepted["moods"]}})
    if "flavor_profiles" not in dropped and accepted["flavor_profiles"]:
        filters.append({"terms": {"flavor_profiles": accepted["flavor_profiles"]}})
    if "food_groups" not in dropped and accepted["food_groups"]:
        filters.append({"terms": {"food_groups": accepted["food_groups"]}})
    if "max_minutes" not in dropped and payload.max_minutes:
        filters.append({"range": {"duration": {"lte": payload.max_minutes}}})

    return filters


CARD_FIELDS = [
    "recipe_id", "title", "image_url", "url", "source", "duration", "serves",
    "course_types", "cuisines", "moods", "flavor_profiles", "food_groups",
    "diet_tags", "allergens", "default_nutri_score", "expert_recipe",
    # Recipe text. A planning surface that returns only cards forces its caller
    # to make a second round of detail lookups before it can do anything with
    # the plan — FoodChat's grader reads ingredients and directions to judge a
    # combination, so without these `plan_meals` cannot be its candidate source
    # at all. Returned flattened by `_plan_card` below.
    "ingredients", "instructions",
]


def _plan_card(recipe: dict[str, Any]) -> dict[str, Any]:
    """Flatten a search hit into the card a planner consumer expects.

    `ingredients` is nested in the index (`[{name, position}, ...]`) and
    `instructions` may be a list. Both are flattened to strings here because
    every consumer wants them that way — an LLM grader reads them as prose, and
    a client rendering them joins them anyway. Leaving the nested shape on the
    wire would push the same flattening into every caller.
    """
    ingredients = recipe.get("ingredients")
    if isinstance(ingredients, (list, tuple)):
        names = [
            item.get("name") if isinstance(item, dict) else item
            for item in ingredients
        ]
        recipe["ingredients"] = ", ".join(
            str(name).strip() for name in names if name
        )

    instructions = recipe.get("instructions")
    if isinstance(instructions, (list, tuple)):
        recipe["instructions"] = "\n".join(
            str(part).strip() for part in instructions if part
        )
    # `directions` is the name FoodChat's candidate contract uses; carried
    # alongside `instructions` so both vocabularies read the same text.
    recipe["directions"] = recipe.get("instructions") or ""
    return recipe


@router.get("", summary="What RecipeWrangler offers — capabilities and tool schemas")
def tool_manifest() -> dict[str, Any]:
    """Self-describing manifest for an LLM agent.

    Returned rather than hardcoded in the agent so that the option lists cannot
    drift out of sync with the corpus: the vocabularies here are the same
    objects the annotation pipeline validates against.
    """
    entity = recipe_entity()
    try:
        total = entity.count()
    except Exception:  # noqa: BLE001
        total = None

    return {
        "service": "RecipeWrangler",
        "description": (
            "A curated recipe corpus with nutrition profiling, allergen "
            "evidence and discovery annotations. Every recipe carries a "
            "Nutri-Score, an allergen list derived from ingredient-level "
            "declarations, and classification into course type, cuisine, "
            "flavour profile, mood and food groups."
        ),
        "corpus": {
            "recipes": total,
            "sources": [
                {"slug": s.slug, "name": s.display_name, "curated": s.curated}
                for s in S.active_sources()
            ],
            "coverage_note": (
                "course_types 100%, moods and flavour ~100%, cuisines 86% "
                "(the remainder are deliberate abstentions where the dish has "
                "no identifiable tradition), food_groups 90% (limited by "
                "ingredient-to-ontology linkage)."
            ),
        },
        "capabilities": [
            "Filter by course, cuisine, mood, flavour, food group, diet and allergen",
            "Exclude allergens using ingredient-level evidence, not just labels",
            "Rank or filter by Nutri-Score",
            "Build multi-day, multi-slot meal plans with variety control",
            "Relax soft preferences automatically and report what was relaxed",
        ],
        "limitations": [
            "Allergen data is derived from ingredient declarations and is not a "
            "substitute for reading the recipe; treat it as advisory.",
            "Cuisine, mood and flavour are model-assigned from a closed "
            "vocabulary; provenance is recorded per value in annotation_evidence.",
            "Vegan/vegetarian suitability is three-state; 'unknown' is not 'no'.",
        ],
        "vocabularies": {
            "meal_slots": sorted(SLOT_COURSE_TYPES),
            "course_types": list(S.COURSE_TYPES),
            "cuisines": list(V.CUISINES),
            "moods": list(V.MOODS),
            "flavor_profiles": list(V.FLAVOR_PROFILES),
            "food_groups": list(V.FOOD_GROUPS),
            "tags": list(V.PLANNING_TAGS),
            "nutri_scores": list(NUTRI_RANK),
        },
        "tools": [
            {
                "name": "plan_meals",
                "endpoint": "POST /api/v2/tools/plan_meals",
                "description": (
                    "Fill named meal slots across one or more days, honouring "
                    "cuisine/mood/flavour preferences, dietary requirements and "
                    "allergen exclusions. Never repeats a recipe within a plan. "
                    "If constraints are too narrow, drops soft preferences in "
                    "order (moods, flavours, cuisines, time) and reports each "
                    "drop; allergen and diet constraints are never relaxed."
                ),
                "required": ["slots"],
            },
            {
                "name": "find_recipes",
                "endpoint": "POST /api/v2/tools/find_recipes",
                "description": (
                    "Free-text search with the same optional filters. Text "
                    "ranks results; filters narrow them. Use when the user "
                    "names a dish rather than asking for a plan."
                ),
                "required": [],
            },
            {
                "name": "describe_options",
                "endpoint": "GET /api/v2/tools",
                "description": "This document. Call it to discover valid option values.",
                "required": [],
            },
        ],
        "relaxation_order": list(RELAXATION_ORDER),
        # Every constraint the ladder cannot touch, not just the three that were
        # listed. `include_ingredients`, `min_nutri_score`, `sources` and
        # `exclude_recipe_ids` were as hard as allergens in behaviour while the
        # manifest implied they might be surrendered — an agent reading this to
        # decide what to send was being told the wrong thing.
        "never_relaxed": [
            "exclude_allergens", "exclude_ingredients", "diet",
            "include_ingredients", "min_nutri_score", "sources",
            "exclude_recipe_ids", "course_types",
        ],
    }


def _attach_plan_nutrition(days: list[dict[str, Any]]) -> None:
    """Add `nutrition` to every recipe and `nutrition_total` to every day.

    Modifies in place. Best-effort: a nutrition outage leaves the plan without
    numbers rather than without meals, because the recipes themselves are still
    perfectly good recommendations.

    Totals sum only what is known. A day containing one unprofiled recipe
    reports the total of the rest plus `complete: false` — silently returning a
    total that omits a meal would be a number someone might act on.
    """
    from recipe_wrangler.catalog.nutrition import per_serving

    recipe_ids = [
        str(recipe["recipe_id"])
        for day in days
        for slot in day.get("slots", [])
        for recipe in slot.get("recipes", [])
        if recipe.get("recipe_id")
    ]
    if not recipe_ids:
        return

    try:
        from recipe_wrangler.repositories.postgres_nutrition import (
            get_recipe_nutrition_batch,
        )

        raw_by_id = get_recipe_nutrition_batch(recipe_ids) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan nutrition lookup failed for %d recipes: %s",
                       len(recipe_ids), exc)
        return

    for day in days:
        totals = {
            "calories": 0.0, "protein_g": 0.0,
            "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0,
        }
        complete = True
        for slot in day.get("slots", []):
            for recipe in slot.get("recipes", []):
                macros = per_serving(raw_by_id.get(str(recipe.get("recipe_id"))))
                recipe["nutrition"] = macros
                if not macros:
                    complete = False
                    continue
                for key in totals:
                    value = macros.get(key)
                    if value is None:
                        complete = False
                    else:
                        totals[key] += value
        day["nutrition_total"] = {
            **{k: round(v, 1) for k, v in totals.items()},
            # False means at least one recipe contributed nothing to the sum.
            "complete": complete,
        }


# How many candidates to fetch per requested one, so variety has a choice.
#
# Five is what it takes for a seven-day breakfast slot: the corpus's mueslis
# sort adjacently, and a smaller window still returned nothing but mueslis to
# choose between. Capped at 100 rows per slot in the call itself.
_VARIETY_OVERSAMPLE = 5

# Never offer the selector fewer than this many candidates, whatever the
# requested count. Sized for the constrained corners: a 7-day vegan plan needs
# seven distinct breakfast families out of one slot's window.
_VARIETY_MIN_POOL = 30


@router.post("/plan_meals", summary="Fill meal slots with variety and graceful relaxation")
def plan_meals(
    payload: MealPlanRequest, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    entity = recipe_entity()
    accepted, rejected = _validate_options(payload)

    used: set[str] = set(payload.exclude_recipe_ids)
    # dish family -> how many plates it already holds, across the whole plan.
    # Shared across days and slots on purpose: the repetition a member notices
    # is "muesli again on Thursday", which is invisible to any per-slot check.
    families: dict[str, int] = {}
    relaxations: list[dict[str, Any]] = []
    days: list[dict[str, Any]] = []

    try:
        for day in range(1, payload.days + 1):
            day_slots: list[dict[str, Any]] = []
            for slot_req in payload.slots:
                slot = slot_req.slot.strip().lower()
                course_types = tuple(
                    S.canonical_course_types(slot_req.course_types)
                ) or SLOT_COURSE_TYPES.get(slot, ())

                dropped: set[str] = set()
                results: list[dict[str, Any]] = []

                # Relaxation ladder: try the full constraint set, then shed one
                # soft preference at a time until something comes back.
                for step in range(len(RELAXATION_ORDER) + 1):
                    filters = _filters(payload, accepted, course_types, dropped)
                    filters.append(
                        {"bool": {"must_not": [{"terms": {"recipe_id": sorted(used)}}]}}
                        if used
                        else {"match_all": {}}
                    )
                    # Favourites float, they do not filter. Expressed as a
                    # `should` so the hard filters above still decide what is
                    # eligible: a favourite that breaks the member's diet must
                    # not reappear because they once liked it.
                    boosts = (
                        [{"terms": {"recipe_id": payload.favorite_recipe_ids, "boost": 10}}]
                        if payload.favorite_recipe_ids
                        else None
                    )
                    found = entity.search(
                        filters=filters,
                        should=boosts,
                        # Over-fetch so `select_diverse` has something to choose
                        # between. Taking exactly `count` from a deterministic
                        # ranking is what produced four mueslis in a seven-day
                        # week: same-kind recipes score alike, so they arrive
                        # adjacent and a top-N slice takes them as a block.
                        #
                        # The floor matters for multi-day plans: day 7's
                        # breakfast draws from the same window as day 1's, with
                        # six families already spent. A count=1 request that
                        # fetched only 5 was down to repeats by day five on the
                        # vegan corpus.
                        limit=max(min(slot_req.count * _VARIETY_OVERSAMPLE, 100),
                                  _VARIETY_MIN_POOL),
                        # Deterministic: `preferred` recipes first, then best
                        # Nutri-Score, then curated sources, then recipe_id as
                        # a total tiebreak. Without that last key Elasticsearch
                        # orders ties by internal doc order, so the same request
                        # can return a different plan each time — which reads as
                        # a bug to a user regenerating a plan.
                        sort=(
                            # With favourites in play `_score` leads, so a
                            # boosted recipe actually rises; the deterministic
                            # keys still break every tie beneath it. Without
                            # them the order is fully deterministic, which is
                            # what stops a regenerated plan looking random.
                            ["_score"] if boosts else []
                        ) + [
                            {"planning_tier": {"order": "asc"}},
                            {"default_nutri_rank": {"order": "asc", "missing": "_last"}},
                            {"source_rank": "asc"},
                            {"recipe_id": "asc"},
                        ],
                        source_fields=CARD_FIELDS,
                    )
                    # Variety is applied before the sufficiency check, so a slot
                    # that came back full of one family is not mistaken for a
                    # satisfied slot. It never shrinks the result — see
                    # `select_diverse`, which backfills rather than under-fill.
                    cards = [_plan_card(r) for r in found["results"]]
                    # A meal slot must hold a meal. The corpus files "North
                    # African Spice Mix" under `breakfast` and lunch accepts
                    # `starter`, which admits pickles — both legal by
                    # annotation and absurd on a plate. Slots that *ask* for
                    # components (side, snack, dessert, drink) keep them.
                    if slot in MEAL_SLOTS:
                        cards = [c for c in cards if variety.is_meal(c.get("title") or "")]
                    # On a copy: the ladder may run this several times, and an
                    # abandoned attempt must not leave its families counted
                    # against the attempt that actually succeeds. Committed to
                    # `families` once the loop settles.
                    attempt_families = dict(families)
                    results = variety.select_diverse(
                        cards,
                        slot_req.count,
                        seen=attempt_families,
                    )
                    if len(results) >= slot_req.count or not payload.allow_relaxation:
                        break
                    if step < len(RELAXATION_ORDER):
                        candidate = RELAXATION_ORDER[step]
                        # Only report dropping something that was actually set.
                        was_set = (
                            payload.max_minutes
                            if candidate == "max_minutes"
                            else accepted.get(candidate)
                        )
                        if was_set:
                            dropped.add(candidate)
                            relaxations.append(
                                {
                                    "day": day,
                                    "slot": slot,
                                    "dropped": candidate,
                                    "reason": f"only {len(results)} of {slot_req.count} matches with it applied",
                                }
                            )
                        else:
                            dropped.add(candidate)

                families = attempt_families
                for row in results:
                    if row.get("recipe_id"):
                        used.add(str(row["recipe_id"]))

                day_slots.append(
                    {
                        "slot": slot,
                        "course_types": list(course_types),
                        "requested": slot_req.count,
                        "returned": len(results),
                        "relaxed": sorted(d for d in dropped if d),
                        "recipes": results,
                    }
                )
            days.append({"day": day, "slots": day_slots})
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc

    # Per-serving macros, and the day totals they make possible.
    #
    # Elasticsearch carries a Nutri-Score but not the macros — those live in
    # Postgres — so a plan came back with letter grades and no numbers. A caller
    # asking for a day's meals and wanting to know whether they add up to a
    # calorie target had to make a second round of `/recipes/details` calls to
    # find out, which is a strange thing for a meal planner to require.
    #
    # One batched read across every recipe in the plan, after the slots are
    # settled, so it costs a single query regardless of how many days were asked
    # for.
    _attach_plan_nutrition(days)

    envelope = {
        "days": days,
        "applied": {
            "cuisines": accepted["cuisines"],
            "moods": accepted["moods"],
            "flavor_profiles": accepted["flavor_profiles"],
            "food_groups": accepted["food_groups"],
            "tags": accepted.get("tags", []),
            "diet": payload.diet,
            "exclude_allergens": payload.exclude_allergens,
            "max_minutes": payload.max_minutes,
            "min_nutri_score": payload.min_nutri_score,
        },
        "relaxations": relaxations,
        "rejected_options": rejected,
        "total_recipes_used": len(used - set(payload.exclude_recipe_ids)),
    }
    return redact(envelope, caller)


class DraftRecipe(BaseModel):
    """A recipe being written, not yet saved."""

    title: str = Field(..., min_length=1)
    ingredients: list[str] = Field(default_factory=list, description="Ingredient lines or names.")
    description: str | None = None
    instructions: str | None = None
    source: str | None = Field(None, description="Collection slug, if known.")
    tags: list[str] = Field(default_factory=list)


@router.post(
    "/suggest_annotations",
    summary="Suggest course, cuisine, flavour and mood for an unsaved draft",
)
def suggest_annotations(draft: DraftRecipe) -> dict[str, Any]:
    """Classify a draft recipe **without persisting anything**.

    Intended for the creation flow: the author sees suggestions, accepts or
    corrects them, and the confirmed values are sent with the create request.
    A value a person confirmed is better evidence than one a model produced,
    and creation records the difference — confirmed values are written with
    ``method="user_confirmed"`` so a later bulk pass will not overwrite them.

    ``food_groups`` is absent on purpose. It is derived from each ingredient's
    FoodOn class ancestry, which requires the ingredients to have been resolved
    against the graph — that happens on save, not before it. Attempting it here
    would mean guessing, which is the thing this vocabulary exists to avoid.
    """
    from recipe_wrangler.catalog import annotation

    try:
        values, confidence = annotation.suggest(
            title=draft.title,
            ingredients=draft.ingredients,
            description=draft.description,
            tags=draft.tags,
            source=draft.source,
        )
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("annotation model", exc) from exc

    return {
        "suggestions": values,
        "confidence": confidence,
        # Returned so the UI can render alternatives without a second call, and
        # so it can only ever offer values the corpus actually uses.
        "options": {
            "course_types": list(S.COURSE_TYPES),
            "cuisines": list(V.CUISINES),
            "flavor_profiles": list(V.FLAVOR_PROFILES),
            "moods": list(V.MOODS),
        },
        "notes": [
            "Suggestions only — nothing has been saved.",
            "Send the accepted values back with the create request; they will be "
            "recorded as user_confirmed rather than model-derived.",
            "food_groups is derived from ingredient ontology links on save, so it "
            "is not suggested here.",
            "Nutrition profiling runs after creation; it is not part of annotation.",
        ],
        "classification_version": V.CLASSIFICATION_VERSION,
    }


class FindRecipesRequest(MealPlanRequest):
    """Same filters as a plan, plus free text — and no slots.

    Two inherited fields do not apply here and are documented rather than
    silently honoured: `days` (there is no per-day reuse to avoid in a flat
    search) and `allow_relaxation` (there is no ladder — a search returns what
    matches, and an empty result is a true answer rather than a starved slot).
    `favorite_recipe_ids` DOES apply and used to be accepted and ignored, which
    is the one thing this module's own rule forbids.
    """

    slots: list[MealSlotRequest] = Field(default_factory=list, exclude=True)
    q: str | None = Field(None, description="Free text. Ranks results; never filters them.")
    course_types: list[str] = Field(default_factory=list)
    limit: int = Field(10, ge=1, le=100)
    offset: int = Field(0, ge=0)
    facets: list[str] = Field(
        default_factory=list,
        description="Facet counts to return, e.g. ['cuisines','course_types'].",
    )


@router.post("/find_recipes", summary="Free-text recipe search with structured filters")
def find_recipes(
    payload: FindRecipesRequest, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    entity = recipe_entity()
    accepted, rejected = _validate_options(payload)
    course_types = tuple(S.canonical_course_types(payload.course_types))

    # Favourites float, exactly as they do in a plan. The field was inherited,
    # validated, and then dropped — so a member searching for a dish they have
    # favourited got it ranked as though they never had. `minimum_should_match`
    # is deliberately absent: setting it would turn a boost into a
    # favourites-only filter.
    boosts = (
        [{"terms": {"recipe_id": payload.favorite_recipe_ids, "boost": 10}}]
        if payload.favorite_recipe_ids
        else None
    )

    try:
        found = entity.search(
            q=payload.q or None,
            filters=_filters(payload, accepted, course_types, dropped=set()),
            should=boosts,
            limit=payload.limit,
            offset=payload.offset,
            source_fields=CARD_FIELDS,
            facets=payload.facets or None,
            # Every sort ends in `recipe_id` so paging is stable. With a
            # non-total ordering, two documents tied on every sort key can swap
            # places between requests, which makes page 2 drop or repeat rows
            # that page 1 already showed.
            sort=[{"_score": "desc"}, {"recipe_id": "asc"}]
            if (payload.q or boosts)
            else [
                {"default_nutri_rank": {"order": "asc", "missing": "_last"}},
                {"source_rank": "asc"},
                {"recipe_id": "asc"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc

    found["results"] = [_plan_card(r) for r in found.get("results", [])]
    found["rejected_options"] = rejected
    return redact(found, caller)
