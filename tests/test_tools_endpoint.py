"""The agent tool surface.

Two properties matter more than the rest and are tested hardest:

1. **Allergen and diet constraints are never relaxed.** Everything else is a
   preference the planner may surrender to fill a slot; an allergen exclusion
   is a safety boundary. If honouring it means returning nothing, nothing is
   the correct answer.
2. **Unrecognised options are reported.** An agent that asks for a cuisine the
   corpus does not classify must be told, so it can say so, rather than
   receiving an unexplained empty list.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.api.routers.tools import (
    NUTRI_RANK,
    RELAXATION_ORDER,
    SLOT_COURSE_TYPES,
    FindRecipesRequest,
    MealPlanRequest,
    MealSlotRequest,
    _filters,
    _validate_options,
    tool_manifest,
)
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog import vocabularies as V


def plan(**kw) -> MealPlanRequest:
    kw.setdefault("slots", [MealSlotRequest(slot="dinner")])
    return MealPlanRequest(**kw)


def flat(filters: list[dict]) -> str:
    import json

    return json.dumps(filters)


class TestRelaxationSafety:
    def test_allergens_are_not_relaxable(self):
        assert "exclude_allergens" not in RELAXATION_ORDER

    def test_diet_is_not_relaxable(self):
        assert "diet" not in RELAXATION_ORDER

    def test_excluded_ingredients_are_not_relaxable(self):
        assert "exclude_ingredients" not in RELAXATION_ORDER

    def test_only_preferences_are_relaxable(self):
        """Nothing safety-bearing may enter the ladder.

        Asserts the property rather than an exact list. The list is expected to
        grow — `food_groups` joined it once it turned out to be applied
        unconditionally, which made it a hard filter in behaviour while the
        manifest advertised it as a preference — and a test pinned to a literal
        set fails on every legitimate addition while still not catching the one
        thing that matters.
        """
        never_relaxed = {
            "exclude_allergens",
            "exclude_ingredients",
            "diet",
            "include_ingredients",
            "min_nutri_score",
            "exclude_recipe_ids",
        }
        assert not (set(RELAXATION_ORDER) & never_relaxed)

    def test_the_ladder_matches_what_the_manifest_promises(self):
        """`never_relaxed` in the manifest is a claim about this tuple.

        They drifted once: `food_groups` was applied unconditionally and absent
        from the ladder, so the service described itself as relaxing something
        it never relaxed.
        """
        from recipe_wrangler.api.routers.tools import tool_manifest

        manifest = tool_manifest()
        for name in manifest["never_relaxed"]:
            assert name not in RELAXATION_ORDER, (
                f"{name} is advertised as never relaxed but is in the ladder"
            )
        assert manifest["relaxation_order"] == list(RELAXATION_ORDER)

    def test_allergen_filter_survives_full_relaxation(self):
        """Even with every relaxable constraint dropped, the allergen
        exclusion must still be in the query."""
        payload = plan(
            exclude_allergens=["peanut"],
            cuisines=["thai"],
            moods=["comfort"],
            max_minutes=10,
        )
        accepted, _ = _validate_options(payload)
        filters = _filters(
            payload, accepted, ("main-dish",), dropped=set(RELAXATION_ORDER)
        )
        assert "peanut" in flat(filters)
        assert "must_not" in flat(filters)

    def test_diet_filter_survives_full_relaxation(self):
        payload = plan(diet=["vegetarian"], cuisines=["thai"])
        accepted, _ = _validate_options(payload)
        filters = _filters(
            payload, accepted, ("main-dish",), dropped=set(RELAXATION_ORDER)
        )
        assert "vegetarian" in flat(filters)

    def test_excluded_ingredient_survives_full_relaxation(self):
        payload = plan(exclude_ingredients=["chicken"], moods=["comfort"])
        accepted, _ = _validate_options(payload)
        filters = _filters(
            payload, accepted, ("main-dish",), dropped=set(RELAXATION_ORDER)
        )
        assert "chicken" in flat(filters)

    @pytest.mark.parametrize("facet", ["cuisines", "moods", "flavor_profiles"])
    def test_preferences_disappear_when_dropped(self, facet):
        payload = plan(cuisines=["thai"], moods=["comfort"], flavor_profiles=["spicy"])
        accepted, _ = _validate_options(payload)
        with_it = flat(_filters(payload, accepted, (), dropped=set()))
        without = flat(_filters(payload, accepted, (), dropped={facet}))
        assert len(without) < len(with_it)


class TestDietFallback:
    def test_vegetarian_matches_evidence_or_tag(self):
        """`suitable_for` is unpopulated (the vegan/vegetarian classifier has
        never been run), so filtering on it alone returns zero."""
        payload = plan(diet=["vegetarian"])
        accepted, _ = _validate_options(payload)
        blob = flat(_filters(payload, accepted, (), dropped=set()))
        assert "suitable_for" in blob
        assert "diet_tags" in blob

    def test_other_diets_use_diet_tags_only(self):
        payload = plan(diet=["gluten-free"])
        accepted, _ = _validate_options(payload)
        blob = flat(_filters(payload, accepted, (), dropped=set()))
        assert "gluten_free" in blob
        assert "suitable_for" not in blob


class TestOptionValidation:
    def test_unknown_values_are_rejected_not_ignored(self):
        payload = plan(cuisines=["fusion", "italian"], moods=["hangry"])
        accepted, rejected = _validate_options(payload)
        assert accepted["cuisines"] == ["italian"]
        assert "cuisines=fusion" in rejected
        assert "moods=hangry" in rejected

    def test_unknown_source_rejected(self):
        _, rejected = _validate_options(plan(sources=["nonexistent"]))
        assert "sources=nonexistent" in rejected

    def test_known_source_resolves_to_raw_value(self):
        accepted, rejected = _validate_options(plan(sources=["foodhero"]))
        assert accepted["sources"] == ["FoodHero"]
        assert rejected == []

    def test_case_and_spacing_tolerated(self):
        accepted, rejected = _validate_options(
            plan(cuisines=["  ITALIAN  "], moods=["Comfort"])
        )
        assert accepted["cuisines"] == ["italian"]
        assert accepted["moods"] == ["comfort"]
        assert rejected == []


class TestSlotMapping:
    def test_every_slot_maps_to_real_course_types(self):
        for slot, courses in SLOT_COURSE_TYPES.items():
            assert courses, slot
            for course in courses:
                assert course in S.COURSE_TYPES, f"{slot} -> {course}"

    def test_lunch_and_dinner_are_not_identical(self):
        """Lunch admits starters and salads; dinner is narrower."""
        assert SLOT_COURSE_TYPES["lunch"] != SLOT_COURSE_TYPES["dinner"]

    def test_breakfast_is_specific(self):
        assert SLOT_COURSE_TYPES["breakfast"] == ("breakfast",)


class TestNutriScoreFilter:
    def test_ranks_are_ordered_best_first(self):
        assert NUTRI_RANK["A"] < NUTRI_RANK["C"] < NUTRI_RANK["E"]

    def test_min_score_is_an_upper_bound_on_rank(self):
        payload = plan(min_nutri_score="B")
        accepted, _ = _validate_options(payload)
        blob = flat(_filters(payload, accepted, (), dropped=set()))
        assert '"lte": 2' in blob


class TestManifest:
    def test_lists_every_tool_with_an_endpoint(self):
        manifest = tool_manifest()
        names = {t["name"] for t in manifest["tools"]}
        assert {
            "plan_meals", "find_recipes", "describe_options", "food_safety"
        } <= names
        for tool in manifest["tools"]:
            assert tool["endpoint"].split()[0] in {"GET", "POST"}

    def test_vocabularies_match_the_annotation_pipeline(self):
        """The manifest must not drift from what annotation validates against,
        or an agent will offer options no recipe can have."""
        vocab = tool_manifest()["vocabularies"]
        assert vocab["cuisines"] == list(V.CUISINES)
        assert vocab["moods"] == list(V.MOODS)
        assert vocab["flavor_profiles"] == list(V.FLAVOR_PROFILES)
        assert vocab["course_types"] == list(S.COURSE_TYPES)

    def test_declares_what_is_never_relaxed(self):
        """Every constraint the ladder cannot touch, not just the three that
        were listed. `include_ingredients`, `min_nutri_score`, `sources`,
        `exclude_recipe_ids` and `course_types` were as hard as allergens in
        behaviour while the manifest implied they might be surrendered — an
        agent reading this to decide what to send was told the wrong thing."""
        manifest = tool_manifest()
        assert set(manifest["never_relaxed"]) == {
            "exclude_allergens",
            "exclude_ingredients",
            "diet",
            "include_ingredients",
            "min_nutri_score",
            "sources",
            "exclude_recipe_ids",
            "course_types",
        }

    def test_never_relaxed_and_the_ladder_do_not_overlap(self):
        """The two lists are the whole contract: anything in one must not be in
        the other, or the manifest contradicts itself."""
        manifest = tool_manifest()
        assert set(manifest["never_relaxed"]).isdisjoint(
            set(manifest["relaxation_order"])
        )

    def test_states_its_limitations(self):
        """An agent relaying allergen data to a user needs to know it is
        advisory and that 'unknown' suitability is not 'no'."""
        text = " ".join(tool_manifest()["limitations"]).lower()
        assert "allergen" in text
        assert "unknown" in text

    def test_describes_course_coverage_and_optional_abstentions(self):
        note = tool_manifest()["corpus"]["coverage_note"].lower()
        assert "classified course type" in note
        assert "can be empty" in note
        assert "no defensible value" in note


class TestFindRecipesRequest:
    def test_slots_not_required(self):
        assert FindRecipesRequest(q="pasta").slots == []

    def test_inherits_the_same_filters(self):
        req = FindRecipesRequest(q="curry", diet=["vegan"], exclude_allergens=["peanut"])
        assert req.diet == ["vegan"]
        assert req.exclude_allergens == ["peanut"]


# ---------------------------------------------------------------------------
# Paging: the window has to be movable, and the manifest has to say so
# ---------------------------------------------------------------------------

def test_plan_meals_accepts_an_offset():
    """The ranking is deterministic, so a caller that cannot move the window
    gets the same plan every time it asks."""
    from recipe_wrangler.api.routers.tools import MealPlanRequest

    assert "offset" in MealPlanRequest.model_fields
    assert MealPlanRequest(slots=[]).offset == 0


def test_a_negative_offset_is_refused():
    import pytest
    from pydantic import ValidationError

    from recipe_wrangler.api.routers.tools import MealPlanRequest

    with pytest.raises(ValidationError):
        MealPlanRequest(slots=[], offset=-1)


def test_the_manifest_says_which_fields_each_tool_takes():
    """Unknown fields are a 422 here, so a caller has no safe way to try an
    optional parameter and see — it either knows the field exists or it cannot
    send it. The manifest is the only thing that can tell it."""
    from recipe_wrangler.api.routers.tools import (
        FindRecipesRequest,
        MealPlanRequest,
        tool_manifest,
    )

    tools = {t["name"]: t for t in tool_manifest()["tools"]}
    assert set(tools["plan_meals"]["accepts"]) == set(MealPlanRequest.model_fields)
    assert set(tools["find_recipes"]["accepts"]) == set(FindRecipesRequest.model_fields)
    assert "offset" in tools["plan_meals"]["accepts"]


def test_accepts_is_generated_from_the_model_not_written_out():
    """A hand-maintained list drifts from the model the first time someone adds
    a field — and then a caller is told it cannot send something it can, or
    told it can send something that 422s."""
    import inspect

    from recipe_wrangler.api.routers import tools

    source = inspect.getsource(tools.tool_manifest)
    assert "sorted(MealPlanRequest.model_fields)" in source
    assert "sorted(FindRecipesRequest.model_fields)" in source
