"""The 2026-09-16 curation pass: a stock cube must not be scaled at the
full weight of the liquid it seasons ("850g water and 1 vegetable stock
cube" resolving to 850g of stock-cube nutrition -> ~47,600mg sodium)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from recipe_wrangler.tools import nutritional_calculator as calc


class CappedConcentrateWeightTests(unittest.TestCase):
    def test_high_sodium_large_weight_is_capped(self):
        weight_g, capped = calc._capped_concentrate_weight_g(850.0, 20000.0, "Stock cubes, chicken")
        self.assertTrue(capped)
        self.assertEqual(weight_g, calc._CONCENTRATE_CAPPED_WEIGHT_G)

    def test_high_sodium_small_weight_is_not_capped(self):
        # A real 10g stock cube already carries a small weight -- nothing to cap.
        weight_g, capped = calc._capped_concentrate_weight_g(10.0, 20000.0, "Stock cubes, chicken")
        self.assertFalse(capped)
        self.assertEqual(weight_g, 10.0)

    def test_large_weight_of_ordinary_food_is_not_capped(self):
        # 850g of a normal low-sodium food (e.g. water) is plausible and must
        # not be touched.
        weight_g, capped = calc._capped_concentrate_weight_g(850.0, 5.0, "Water, municipal")
        self.assertFalse(capped)
        self.assertEqual(weight_g, 850.0)

    def test_high_sodium_large_weight_without_concentrate_marker_is_not_capped(self):
        # Found via the plausibility audit: "dried noodles" matched to
        # "Soup, chicken noodle, dried" (a real bad match, but a separate
        # matching bug) -- 250-375g of noodles is a plausible weight and must
        # not be crushed to 15g just because that record's sodium is dense.
        # Same protects anchovies/fish sauce/soy sauce/cured meats in bulk.
        weight_g, capped = calc._capped_concentrate_weight_g(
            300.0, 3740.0, "Soup, chicken noodle, dried"
        )
        self.assertFalse(capped)
        self.assertEqual(weight_g, 300.0)

    def test_concentrate_marker_variants_are_capped(self):
        for matched_name in (
            "Stock gel", "Broth or stock, beef, dehydrated",
            "Stock cubes, vegetable", "Bouillon granules", "Gravy powder",
        ):
            with self.subTest(matched_name=matched_name):
                weight_g, capped = calc._capped_concentrate_weight_g(
                    500.0, 5000.0, matched_name
                )
                self.assertTrue(capped)


class CappedSeasoningWeightTests(unittest.TestCase):
    """17% of the plausibility audit's flagged lines: a "to taste" salt/pepper
    line getting a real gram weight instead of a pinch."""

    def test_salt_and_pepper_variants_are_capped(self):
        for name, weight_g in (
            ("salt and pepper", 720 / 7),
            ("black pepper", 750.0),
            ("black pepper", 436.0),
            ("salt", 500.0),
        ):
            with self.subTest(name=name, weight_g=weight_g):
                capped_weight, capped = calc._capped_seasoning_weight_g(weight_g, name)
                self.assertTrue(capped)
                self.assertEqual(capped_weight, calc._SEASONING_CAPPED_WEIGHT_G)

    def test_other_spice_herb_class_names_are_capped(self):
        weight_g, capped = calc._capped_seasoning_weight_g(50.0, "cinnamon")
        self.assertTrue(capped)

    def test_bell_pepper_is_not_capped(self):
        # "pepper" alone is deliberately ambiguous in food_class() (also
        # means bell pepper/capsicum) -- must not crush a real vegetable
        # weight. Only "black pepper"/"peppercorn" are gated explicitly.
        weight_g, capped = calc._capped_seasoning_weight_g(200.0, "bell pepper")
        self.assertFalse(capped)
        self.assertEqual(weight_g, 200.0)

    def test_salt_free_stock_is_not_treated_as_a_seasoning_line(self):
        # "salt-free vegetable stock" contains the substring "salt" but names
        # a stock, not a seasoning -- found while re-checking the corpus after
        # adding this cap. That line's weight is the concentrate cap's job.
        weight_g, capped = calc._capped_seasoning_weight_g(400.0, "salt-free vegetable stock")
        self.assertFalse(capped)
        self.assertEqual(weight_g, 400.0)

    def test_small_seasoning_weight_is_left_alone(self):
        weight_g, capped = calc._capped_seasoning_weight_g(10.0, "salt")
        self.assertFalse(capped)
        self.assertEqual(weight_g, 10.0)


class PepperContextTests(unittest.TestCase):
    def test_bare_pepper_uses_measurement_context(self):
        cases = (
            ("1/4 teaspoon", 0.6, "black pepper"),
            ("to taste", 1.0, "black pepper"),
            ("1 medium", 120.0, "sweet pepper"),
            ("150 g", 150.0, "sweet pepper"),
            ("1", 120.0, "sweet pepper"),
        )
        for measurement, weight_g, expected in cases:
            with self.subTest(measurement=measurement):
                self.assertEqual(
                    calc._contextual_match_name(
                        "pepper", "pepper", measurement, weight_g
                    ),
                    expected,
                )


class SoupSodiumRegressionTests(unittest.TestCase):
    """Integration-level: the compound "water + stock cube" line, mis-matched
    entirely to the stock cube (the parser bug this backstops, not fixes —
    see plan Step 3), no longer produces implausible per-serving sodium."""

    def test_stock_cube_line_at_full_liquid_weight_is_capped_before_scaling(self):
        match = {
            "metadata": {"eu_id": "cofid:17-726", "title": "Stock cubes, chicken"},
            "document": "Stock cubes, chicken",
        }
        nutrient_row = {
            "nutrients": {
                "Sodium, Na": {"value": 20000.0},
                "Energy": {"value": 400.0},
                "Protein": {"value": 5.0},
                "Carbohydrate, by difference": {"value": 10.0},
                "Total lipid (fat)": {"value": 2.0},
            }
        }
        with patch.object(
            calc, "best_nutrition_match",
            return_value={
                "match": match, "source_key": "eu", "similarity": None,
                "confidence": "curated", "reason": "alias",
                "matched_name": "Stock cubes, chicken", "cleaned_query": "vegetable stock cube",
            },
        ), patch.object(calc, "get_eu_ingredient_nutrition", return_value=nutrient_row):
            result = calc.nutritional_tool_vector.invoke({
                "title": "Vegetable soup",
                "ingredient_names": ["vegetable stock cube"],
                "weights": [850.0],
                "source": "eu",
                "serves": 4,
            })
        detail = result["details"][0]
        self.assertTrue(detail["weight_capped"])
        self.assertEqual(detail["weight_g"], calc._CONCENTRATE_CAPPED_WEIGHT_G)
        self.assertEqual(detail["original_weight_g"], 850.0)
        # 20000mg/100g * 15g/100 = 3000mg total, not 20000*8.5=170000mg.
        self.assertAlmostEqual(result["clean_totals"]["sodium_mg"], 3000.0, places=1)


if __name__ == "__main__":
    unittest.main()
