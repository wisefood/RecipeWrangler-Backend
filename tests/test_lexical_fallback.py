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
    _QUESTION_SIGNAL_KEYS,
    question_yielded_nothing,
)


def signals(**overrides):
    """A question-signal snapshot: everything empty unless named."""
    base = {key: [] for key in _QUESTION_SIGNAL_KEYS}
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
        "sources", "convenience", "nutrition_claims", "nutri_scores",
        "flavor_profiles", "exclude_allergens",
    ])
    def test_caller_only_fields_are_not_consulted(self, facet):
        assert facet not in _QUESTION_SIGNAL_KEYS

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
