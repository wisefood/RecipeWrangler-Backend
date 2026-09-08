"""The lexical fallback: when a typed question must become a filter.

The extractor turns a bare noun like "pasta" into no constraints at all. When
that happens the Elasticsearch query has no lexical clause, matches the whole
corpus, and sorts on expert/source rank — so *every* such search returns the
same list in the same order. The fallback exists to stop that, by using the
raw question as the title query.

What this module guards is the decision to engage it. That decision used to
count a caller's facet chip as evidence the question had been understood, so
one click on a source or cuisine turned every text search back into the
unfiltered corpus: `pasta` and `chocolate cake` with the same chip returned
byte-identical pages, alphabetically ordered, because `rank_query` ranks and
never filters. The bug was invisible — a plausible page of real recipes, and
no error anywhere.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.api.routers.recipes import (
    _NOT_QUESTION_SIGNAL,
    question_yielded_nothing,
)

#: Every facet the extractor can fill from a question. Named here rather than
#: imported so that a facet added to the extractor without being considered
#: here shows up as a failing test rather than as a silent behaviour change.
QUESTION_FACETS = (
    "include_ingredients", "exclude_ingredients", "diet_tags", "dish_types",
    "sources", "cuisines", "moods", "flavor_profiles", "food_groups",
    "convenience", "nutrition_claims", "nutri_scores",
    "title_keywords", "title_query", "max_duration_minutes", "min_servings",
    "sort_by",
)


def signals(**overrides):
    """A question-signal snapshot: everything empty unless named."""
    base = {key: [] for key in QUESTION_FACETS}
    base.update({"max_duration_minutes": None, "min_servings": None,
                 "sort_by": None, "title_query": None, "allergens": []})
    base.update(overrides)
    return base


class TestTheDecision:
    def test_nothing_extracted_engages_the_fallback(self):
        # "pasta": the extractor's classic miss.
        assert question_yielded_nothing(signals()) is True

    @pytest.mark.parametrize("key,value", [
        ("include_ingredients", ["chicken"]),
        ("sources", ["supervalu"]),
        ("convenience", ["quick"]),
        ("nutrition_claims", ["high-protein"]),
        ("nutri_scores", ["A"]),
        ("flavor_profiles", ["savoury"]),
        ("exclude_ingredients", ["nuts"]),
        ("diet_tags", ["vegan"]),
        ("dish_types", ["dessert"]),
        ("title_keywords", ["risotto"]),
        ("title_query", "chocolate cake"),
        ("cuisines", ["italian"]),
        ("moods", ["comforting"]),
        ("food_groups", ["fish"]),
        ("max_duration_minutes", 30),
        ("min_servings", 4),
        ("sort_by", "duration"),
        ("allergens", ["peanut"]),
    ])
    def test_any_question_derived_signal_suppresses_it(self, key, value):
        # These all mean the question was understood; forcing the raw text in
        # as a mandatory title clause would then exclude valid matches —
        # "under 30 minutes" must not require those words in the title.
        assert question_yielded_nothing(signals(**{key: value})) is False


class TestCallerFacetsAreNotSignal:
    """The regression itself: a chip must not disable the fallback."""

    @pytest.mark.parametrize("facet", [
        "exclude_allergens", "boost_tags", "boost_ingredients",
    ])
    def test_caller_supplied_fields_are_not_consulted(self, facet):
        # These carry the member's profile and preference boosts, never a
        # reading of the question.
        assert facet in _NOT_QUESTION_SIGNAL

    def test_the_question_itself_is_not_evidence_it_was_understood(self):
        # rank_query is always set to the raw question, so counting it would
        # mean the fallback could never engage at all.
        assert "rank_query" in _NOT_QUESTION_SIGNAL

    @pytest.mark.parametrize("facet", [
        "sources", "convenience", "nutrition_claims", "nutri_scores",
        "flavor_profiles",
    ])
    def test_facets_the_extractor_fills_do_count(self, facet):
        # These became question-derived when the extractor learned to fill
        # every facet. While they were excluded, a question it had understood
        # ("quick recipes" -> convenience) was treated as unparsed and forced
        # through the title-query path to the whole corpus.
        assert facet not in _NOT_QUESTION_SIGNAL
        assert question_yielded_nothing(signals(**{facet: ["x"]})) is False

    def test_a_chip_alongside_a_bare_noun_still_engages_the_fallback(self):
        # "pasta" + the supervalu chip. The snapshot is taken before caller
        # selections merge, so it stays empty and the question becomes the
        # title query — filtering to pasta *within* supervalu, instead of
        # returning all 68 supervalu recipes for every query typed.
        assert question_yielded_nothing(signals()) is True

    def test_profile_allergens_do_not_count_as_understanding(self):
        # `exclude_allergens` carries the member's profile, merged in long
        # before this point, so the snapshot is built from the extractor's own
        # `allergens` instead and the profile never reaches this decision.
        # Reading the merged field is what used to cost every allergic member
        # the fallback on every search they ran.
        snapshot = signals(allergens=[])
        assert "exclude_allergens" not in snapshot
        assert question_yielded_nothing(snapshot) is True


class TestTheListCannotGoStaleAgain:
    """Every field of `base_constraints` must be a deliberate choice.

    This is the guard for the bug itself, not for its symptom. The signal test
    used to be an allow-list, and when the extractor learned to fill `sources`,
    `convenience`, `nutrition_claims`, `nutri_scores` and `flavor_profiles`
    from the question, the allow-list did not learn with it — so a question
    that had been understood was treated as unparsed, and searches went
    through the title-query path to the whole corpus. Nothing failed; the
    results just got worse.

    Inverting it to an exclusion list fixed that going forward. This makes the
    remaining risk — a new field that is *not* question-derived being counted
    as though it were — fail here instead of in production.
    """

    @staticmethod
    def _base_constraint_fields():
        import inspect
        import re

        from recipe_wrangler.api.routers import recipes

        source = inspect.getsource(recipes.recipe_search)
        start = source.index("base_constraints = dict(")
        body = source[start:]
        # Cut at the closing paren of the dict( call, tracking depth.
        depth, end = 0, None
        for i, ch in enumerate(body):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        assert end is not None, "could not find the end of base_constraints"
        return set(re.findall(r"^\s{8}(\w+)=", body[:end], re.M))

    def test_every_field_is_either_signal_or_deliberately_excluded(self):
        from recipe_wrangler.api.routers.recipes import _NOT_QUESTION_SIGNAL

        fields = self._base_constraint_fields()
        assert fields, "parsed no fields — the guard has stopped guarding"

        counted = fields - _NOT_QUESTION_SIGNAL
        unknown = counted - set(QUESTION_FACETS)
        assert not unknown, (
            "these base_constraints fields now count as evidence that the "
            f"question was understood: {sorted(unknown)}. If that is right, add "
            "them to QUESTION_FACETS in this module. If they come from the "
            "caller rather than the question, add them to _NOT_QUESTION_SIGNAL "
            "— counting a caller's facet chip as understanding is what made "
            "every search return the same page."
        )

    def test_the_exclusions_are_all_real_fields(self):
        from recipe_wrangler.api.routers.recipes import _NOT_QUESTION_SIGNAL

        fields = self._base_constraint_fields()
        stale = {name for name in _NOT_QUESTION_SIGNAL if name not in fields}
        assert not stale, f"excluded fields that no longer exist: {sorted(stale)}"
