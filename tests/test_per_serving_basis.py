"""A whole recipe's nutrition must never be published as one serving's.

Round 2 row 1 and the ESSRG sourdough report: the same recipe read 1,670 kcal
under EU and 230-250 under IE/HU/SI. Profiles are stored one row per region
(PK is recipe_id + nutrition_source) and the row does not record the serving
count it was computed with, so a region whose row has no per-serving block
fell back to the recipe total and showed it unchanged.
"""

import unittest

from recipe_wrangler.api.routers.recipes import _per_serving
from recipe_wrangler.catalog.nutrition import per_serving


class RecipeDetailPerServingTests(unittest.TestCase):
    def test_divides_when_the_serving_count_is_known(self):
        self.assertAlmostEqual(_per_serving(1670.0, 7), 238.57, places=1)

    def test_declines_when_the_serving_count_is_unknown(self):
        # The old behaviour returned 1670.0 here, labelled "per serving".
        for serves in (None, 0, -1, "", "not a number"):
            with self.subTest(serves=serves):
                self.assertIsNone(_per_serving(1670.0, serves))

    def test_a_missing_value_stays_missing(self):
        self.assertIsNone(_per_serving(None, 4))

    def test_a_numeric_string_serving_count_still_divides(self):
        self.assertAlmostEqual(_per_serving(1000.0, "4"), 250.0)


class CatalogPerServingTests(unittest.TestCase):
    def test_uses_the_stored_per_serving_block_when_there_is_one(self):
        macros = per_serving(
            {
                "total_nutrients_per_serving": {"energy_kcal": 238.0, "protein_g": 8.0},
                "total_nutrients": {"energy_kcal": 1670.0, "protein_g": 56.0},
            }
        )
        self.assertAlmostEqual(macros["calories"], 238.0)
        self.assertAlmostEqual(macros["protein_g"], 8.0)

    def test_divides_the_totals_when_a_serving_count_is_supplied(self):
        macros = per_serving({"total_nutrients": {"energy_kcal": 1670.0}}, 7)
        self.assertAlmostEqual(macros["calories"], 238.57, places=1)

    def test_declines_rather_than_hand_the_planner_a_whole_recipe(self):
        # This is the meal-planner hazard: without a serving count these
        # totals used to arrive as one plate's macros.
        self.assertIsNone(per_serving({"total_nutrients": {"energy_kcal": 1670.0}}))
        self.assertIsNone(per_serving({"total_nutrients": {"energy_kcal": 1670.0}}, 0))
        self.assertIsNone(
            per_serving({"total_nutrients": {"energy_kcal": 1670.0}}, "unknown")
        )

    def test_an_empty_per_serving_block_is_treated_as_absent(self):
        self.assertIsNone(per_serving({"total_nutrients_per_serving": {}}))
        macros = per_serving(
            {"total_nutrients_per_serving": {}, "total_nutrients": {"energy_kcal": 800.0}},
            4,
        )
        self.assertAlmostEqual(macros["calories"], 200.0)

    def test_no_nutrition_at_all_returns_none(self):
        self.assertIsNone(per_serving(None, 4))
        self.assertIsNone(per_serving({}, 4))
        self.assertIsNone(per_serving({"total_nutrients": {}}, 4))


class FoodChatCandidateFieldTests(unittest.TestCase):
    def test_candidates_request_the_serving_count(self):
        # per_serving cannot divide without it, so a plan built from recipes
        # whose rows lack a per-serving block would lose its macros entirely.
        from recipe_wrangler.catalog.foodchat import _SOURCE_FIELDS

        self.assertIn("serves", _SOURCE_FIELDS)


if __name__ == "__main__":
    unittest.main()
