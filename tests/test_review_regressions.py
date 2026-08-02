"""Defects from the FoodChat-integration review, pinned so they stay fixed.

Every one of these failed *silently*. The request succeeded, the status code was
200, and the answer was wrong: a disabled recipe served into a meal plan, a
cleared image still showing, a page of search results that was empty while
reporting a non-zero total. None of them would have been caught by a test that
only asserted "it returns something".

So each test below asserts the specific property that was violated, and says
what the wrong behaviour looked like from outside.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.api.routers.tools import RELAXATION_ORDER, _filters, _validate_options
from recipe_wrangler.catalog.foodchat import (
    ALLERGEN_INGREDIENT_TERMS,
    RELAXATION_ORDER as FOODCHAT_LADDER,
    _allergen_exclusions,
    _hard_filters,
    _slot_course_types,
)
from recipe_wrangler.catalog.projection import ES_OWNED_FIELDS, OWNER_PROJECTED_FIELDS
from recipe_wrangler.utils.neo4j_utils import _is_write
from recipe_wrangler.utils.recipe_status import _document_ids


def flatten(node, out=None):
    """Every scalar anywhere in a nested query structure."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            out.append(key)
            flatten(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            flatten(item, out)
    else:
        out.append(node)
    return out


class Req:
    """A FoodChat candidate request, minimally."""

    class _Profile:
        def __init__(self, allergies=(), diet=()):
            self.allergies = list(allergies)
            self.diet = list(diet)

    class _Constraints:
        def __init__(self, **kw):
            self.include_ingredients = kw.get("include_ingredients", [])
            self.exclude_ingredients = kw.get("exclude_ingredients", [])
            self.exclude_recipe_ids = kw.get("exclude_recipe_ids", [])
            self.favorite_recipe_ids = kw.get("favorite_recipe_ids", [])
            self.nutrition_profile = kw.get("nutrition_profile")
            self.cuisines = kw.get("cuisines", [])
            self.moods = kw.get("moods", [])
            self.flavor_profiles = kw.get("flavor_profiles", [])
            self.food_groups = kw.get("food_groups", [])
            self.max_duration_minutes = kw.get("max_duration_minutes")

    def __init__(self, allergies=(), diet=(), **kw):
        self.user_profile = self._Profile(allergies, diet)
        self.constraints = self._Constraints(**kw)
        self.quotas = kw.get("quotas", {"dinner": 3})
        self.randomize = kw.get("randomize", False)


class TestSoftDeleteReachesTheCatalogIndex:
    """Disable silently no-opped on the catalog index for every recipe.

    The bulk update addressed documents by bare recipe id; catalog documents are
    keyed `urn:recipe:<id>`. Elasticsearch answered `document_missing_exception`,
    which the code counted as "not found" — the same bucket as a legitimately
    unindexed recipe. So the job reported success and the withdrawn recipe kept
    being served in search, in browse, and in meal plans.
    """

    def test_both_id_conventions_are_addressed(self):
        assert _document_ids("8310468467") == (
            "8310468467",
            "urn:recipe:8310468467",
        )

    def test_a_urn_input_also_yields_both(self):
        assert set(_document_ids("urn:recipe:abc")) == {"urn:recipe:abc", "abc"}

    def test_blank_ids_address_nothing(self):
        assert _document_ids("   ") == ()


class TestDisabledRecipesAreNeverPlanned:
    """The Elasticsearch candidate path had no status clause at all.

    The Neo4j path it replaces applies `NEO4J_NOT_DISABLED`, so switching to
    Elasticsearch would have made meal planning *less* safe than before —
    serving recipes someone had deliberately withdrawn.
    """

    def test_a_status_exclusion_is_always_present(self):
        flat = flatten(_hard_filters(Req(), ["main-dish"]))

        assert "status" in flat
        assert "disabled" in flat

    def test_it_survives_with_no_constraints_at_all(self):
        assert "disabled" in flatten(_hard_filters(Req(), []))


class TestUnknownSlotsDoNotBecomeUnfiltered:
    """An unmapped slot produced no course filter, so `supper` was filled with
    desserts and beverages — which reads as a working plan rather than as a word
    nobody understood."""

    def test_a_known_slot_maps_to_its_course(self):
        assert _slot_course_types("dinner")

    def test_an_unknown_slot_falls_back_to_main_meal_courses(self):
        courses = _slot_course_types("supper")

        assert courses, "an unknown slot must not disable course filtering"
        assert "main-dish" in courses

    def test_the_fallback_excludes_desserts_and_drinks(self):
        courses = _slot_course_types("elevenses")

        assert "desserts" not in courses
        assert "beverages" not in courses


class TestAllergenExclusionIsSubstantive:
    """A mozzarella recipe with `allergens: []` passed a milk exclusion.

    The clause was a plain analysed `match` on the ingredient name while its
    comment claimed it caught "buttermilk" for milk and "mozzarella cheese" for
    dairy. It did neither: `match` is token-level, and "dairy" and "mozzarella"
    share no token at all — no amount of wildcarding bridges that.
    """

    def test_the_declared_allergen_is_still_excluded(self):
        assert {"term": {"allergens": "milk"}} in _allergen_exclusions("milk")

    def test_substring_matching_is_a_wildcard_not_a_match(self):
        """`match` cannot find "milk" inside "buttermilk"."""
        flat = flatten(_allergen_exclusions("milk"))

        assert "wildcard" in flat
        assert "*milk*" in flat

    def test_dairy_reaches_cheese_by_name(self):
        """The case the old comment claimed and the code could not do."""
        flat = flatten(_allergen_exclusions("dairy"))

        assert "*cheese*" in flat
        assert "*mozzarella*" in flat

    def test_multi_word_allergens_contribute_each_word(self):
        flat = flatten(_allergen_exclusions("tree nuts"))

        assert any("nut" in str(v) for v in flat)

    def test_an_empty_allergen_excludes_nothing(self):
        """Otherwise a blank profile entry would empty every slot."""
        assert _allergen_exclusions("  ") == []

    @pytest.mark.parametrize("allergen", sorted(ALLERGEN_INGREDIENT_TERMS))
    def test_every_mapped_allergen_expands(self, allergen):
        assert len(_allergen_exclusions(allergen)) > 2


class TestFoodGroupsRelax:
    """`food_groups` was applied unconditionally and absent from the ladder, so
    it behaved as a hard filter while the manifest advertised only allergens,
    ingredients and diet as never relaxed. A member asking for fish got an empty
    dinner instead of a relaxed one."""

    def test_food_groups_is_in_the_ladder(self):
        assert "food_groups" in RELAXATION_ORDER

    def test_dropping_it_removes_the_filter(self):
        payload = _plan(food_groups=["fish"])
        accepted, _ = _validate_options(payload)

        applied = flatten(_filters(payload, accepted, ("main-dish",), dropped=set()))
        relaxed = flatten(
            _filters(payload, accepted, ("main-dish",), dropped={"food_groups"})
        )

        assert "fish" in applied
        assert "fish" not in relaxed

    def test_both_planning_paths_relax_in_the_same_order(self):
        """Two ladders that disagree mean the same request degrades differently
        depending on which endpoint served it."""
        strip = lambda ladder: [f for f in ladder if not f.startswith("max_")]  # noqa: E731

        assert strip(RELAXATION_ORDER) == strip(FOODCHAT_LADDER)


class TestClearedFieldsClear:
    """`build_document` strips None and `update` is a partial merge, so a field
    removed on the owner stayed in the index forever — and a re-projection could
    not fix it, because the field was absent from that payload too."""

    def test_owner_projected_and_es_owned_do_not_overlap(self):
        """Clearing an ES-owned field would wipe annotations on the first patch."""
        assert not set(OWNER_PROJECTED_FIELDS) & set(ES_OWNED_FIELDS)

    @pytest.mark.parametrize("field", ["image_url", "url", "description", "instructions"])
    def test_the_fields_that_regressed_are_clearable(self, field):
        assert field in OWNER_PROJECTED_FIELDS

    @pytest.mark.parametrize("field", ["cuisines", "moods", "planning_tier", "creator"])
    def test_annotations_are_not_clearable(self, field):
        assert field not in OWNER_PROJECTED_FIELDS


class TestQueryRouting:
    """Everything went through `execute_write`: a non-idempotent statement could
    be applied twice when the driver retried a transient disconnect, and every
    read was routed to the leader."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (r:Recipe) RETURN r",
            "MATCH (r:Recipe) WHERE r.created_at > 1 RETURN r",
            "MATCH (r) WHERE r.title = 'set the table' RETURN r",
            "// merge the tags\nMATCH (r) RETURN count(r)",
        ],
    )
    def test_reads_are_classified_as_reads(self, query):
        assert not _is_write(query)

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (r) SET r.x = 1",
            "MERGE (r:Recipe {id: 1})",
            "MATCH (r) DETACH DELETE r",
            "CREATE (r:Recipe)",
            "MATCH (r) REMOVE r.x",
            'CALL apoc.periodic.iterate("MATCH (n) RETURN n", "DELETE n", {})',
        ],
    )
    def test_writes_are_classified_as_writes(self, query):
        """Misjudging a write as a read fails loudly — Neo4j refuses it — but
        that is still an outage, so the bias must stay toward `write`."""
        assert _is_write(query)


def _plan(**kw):
    """A minimal `MealPlanRequest`."""
    from recipe_wrangler.api.routers.tools import MealPlanRequest, MealSlotRequest

    return MealPlanRequest(slots=[MealSlotRequest(slot="dinner", count=1)], **kw)


class TestRandomizePreservesRanking:
    """`randomize` returned the identical three recipes on every call.

    First `boost_mode: "replace"` discarded every boost and the `planning_tier`
    sort. Then `multiply` — which looks like the careful fix and is worse: a
    candidate query is almost entirely `filter` clauses, which do not
    contribute to `_score`, so the base score is 0 and 0 x random is 0. Every
    candidate tied, the tiebreak was `recipe_id`, and the shuffle did nothing
    at all. Neither failed; both quietly returned the same plan forever.
    """

    def _body(self, randomize):
        import json

        from recipe_wrangler.catalog import foodchat as F

        captured = {}

        class FakeES:
            alias = "recipes"

            def search_body(self, index, body):
                captured.setdefault("bodies", []).append(json.loads(json.dumps(body)))
                return {"hits": {"hits": []}}

        class FakeEntity:
            es = FakeES()
            alias = "recipes"

        original = F.recipe_entity if hasattr(F, "recipe_entity") else None
        import recipe_wrangler.catalog.entities as entities

        saved = entities.recipe_entity
        entities.recipe_entity = lambda: FakeEntity()
        try:
            F.fetch_candidates_es(Req(randomize=randomize, quotas={"dinner": 3}))
        finally:
            entities.recipe_entity = saved
            if original is not None:
                F.recipe_entity = original
        return captured["bodies"][0]

    def test_randomised_queries_do_not_multiply_a_zero_score(self):
        body = self._body(True)
        function_score = body["query"]["function_score"]

        assert function_score["boost_mode"] != "multiply", (
            "a filter-only query scores 0, so multiplying annihilates the "
            "random component and every call returns the same recipes"
        )
        assert function_score["boost_mode"] != "replace", (
            "replacing the score discards the favourites and ingredient boosts"
        )

    def test_planning_tier_still_leads_the_sort_when_randomised(self):
        """Otherwise an `excluded`-adjacent recipe can outrank a `preferred` one
        purely on noise."""
        body = self._body(True)

        assert body["sort"][0] == {"planning_tier": {"order": "asc"}}

    def test_an_unrandomised_query_has_no_function_score(self):
        assert "function_score" not in self._body(False)["query"]


class TestQuotaIsRespected:
    """Over-fetching for macro filtering leaked the surplus to the caller.

    `attach_nutrition` can only remove candidates, so asking Elasticsearch for
    exactly the quota returned a short slot whenever a nutrition profile was
    set. Over-fetching fixed that and introduced the opposite bug: a request for
    5 breakfasts came back with 13, because nothing trimmed the pool afterwards.
    """

    def test_oversampling_only_applies_with_a_macro_target(self):
        from recipe_wrangler.catalog.foodchat import _MACRO_OVERSAMPLE

        assert _MACRO_OVERSAMPLE > 1
