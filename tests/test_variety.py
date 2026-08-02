"""Variety across a plan, and the week that motivated it.

A seven-day vegan plan returned sixty-three distinct `recipe_id`s and this for
breakfast:

    Day 1  Gluten-free berry muesli
    Day 2  Nutty toasted muesli
    Day 3  Gluten-free muesli
    Day 7  Toasted muesli

The id check saw four different recipes. The member sees muesli. These tests
pin the difference.
"""

from __future__ import annotations

import pytest

from recipe_wrangler.catalog.variety import (
    dish_family,
    family_counts,
    select_diverse,
)


class TestDishFamily:
    @pytest.mark.parametrize(
        "title",
        [
            "Gluten-free berry muesli",
            "Nutty toasted muesli",
            "Gluten-free muesli",
            "Toasted muesli",
            "Munchy muesli mix",  # trailing qualifier still resolves to muesli
        ],
    )
    def test_the_four_breakfasts_are_one_family(self, title):
        assert dish_family(title) == "muesli"

    @pytest.mark.parametrize(
        "title,family",
        [
            # The head noun is what the dish *is*, not the last thing listed.
            ("Baked eggplant with cranberry and mint", "eggplant"),
            ("Spinach with raisins and pine nuts", "spinach"),
            ("Orecchiette with roasted cauliflower, pine nuts", "orecchiette"),
            # A comma is not a boundary: splitting here would give "tomato" and
            # lose the only word that says what the dish is.
            ("Tomato, chickpea and barley salad", "salad"),
            ("Roasted tomato and red lentil soup", "soup"),
            # Plurals collapse onto the singular.
            ("Roasted balsamic thyme onions", "onion"),
            ("Roasted Mediterranean vegetables", "vegetable"),
            ("Sweet Potatoes", "potato"),
        ],
    )
    def test_head_noun(self, title, family):
        assert dish_family(title) == family

    def test_an_all_qualifier_title_does_not_collapse_into_one_family(self):
        """Returning "" for these would file every one of them together."""
        assert dish_family("Slow-roasted") != ""
        assert dish_family("Slow-roasted") != dish_family("Pan-fried")

    @pytest.mark.parametrize("junk", ["", "   ", None, "!!!"])
    def test_unreadable_titles_are_empty_not_an_error(self, junk):
        assert dish_family(junk) == ""


class TestSelectDiverse:
    @staticmethod
    def _rows(*titles):
        return [{"title": t, "recipe_id": t} for t in titles]

    def test_the_reported_week_stops_being_four_mueslis(self):
        rows = self._rows(
            "Gluten-free berry muesli",
            "Nutty toasted muesli",
            "Gluten-free muesli",
            "Breakfast bruschetta",
            "Savory Sweet Potatoes",
        )
        picked = [r["title"] for r in select_diverse(rows, 3)]

        assert family_counts(picked)["muesli"] == 1
        assert len(picked) == 3

    def test_ranking_still_decides_which_one(self):
        """Variety chooses *when to skip*, never what is good."""
        rows = self._rows("Gluten-free berry muesli", "Nutty toasted muesli", "Toast")
        picked = [r["title"] for r in select_diverse(rows, 2)]

        assert picked[0] == "Gluten-free berry muesli", "the best-ranked muesli wins"
        assert picked[1] == "Toast"

    def test_a_short_plan_is_worse_than_a_repetitive_one(self):
        """When the corpus has only one family, still fill the slot."""
        rows = self._rows("Toasted muesli", "Gluten-free muesli", "Berry muesli")
        picked = select_diverse(rows, 3)

        assert len(picked) == 3, "backfill rather than under-fill"

    def test_backfill_preserves_rank_order(self):
        rows = self._rows("Muesli one", "Muesli two", "Muesli three")
        picked = [r["title"] for r in select_diverse(rows, 3)]

        assert picked == ["Muesli one", "Muesli two", "Muesli three"]

    def test_seen_carries_across_slots(self):
        """Day 7's breakfast has to know about day 1's."""
        seen: dict[str, int] = {}
        select_diverse(self._rows("Toasted muesli", "Toast"), 1, seen=seen)
        later = select_diverse(self._rows("Berry muesli", "Porridge"), 1, seen=seen)

        assert later[0]["title"] == "Porridge"

    def test_a_fresh_dict_scopes_variety_to_one_slot(self):
        first = select_diverse(self._rows("Toasted muesli"), 1, seen={})
        second = select_diverse(self._rows("Berry muesli"), 1, seen={})

        assert first and second, "independent dicts do not constrain each other"

    def test_max_per_family_above_one_allows_a_deliberate_repeat(self):
        rows = self._rows("Muesli one", "Muesli two", "Muesli three")
        picked = select_diverse(rows, 3, seen={}, max_per_family=2)

        assert len(picked) == 3

    def test_requesting_more_than_exists_returns_what_there_is(self):
        assert len(select_diverse(self._rows("Toast"), 5)) == 1

    def test_no_candidates_is_not_an_error(self):
        assert select_diverse([], 3) == []
