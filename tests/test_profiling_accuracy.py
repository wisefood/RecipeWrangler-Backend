import unittest
from unittest.mock import patch

from recipe_wrangler.tools import recipe_profiling_tool as rpt
from recipe_wrangler.tools import sustainability_calculator as sc


class ServesSanitizeTests(unittest.TestCase):
    def test_plausible_value_is_kept(self):
        self.assertEqual(rpt._sanitize_serves(4, 1800), (4.0, "given"))
        self.assertEqual(rpt._sanitize_serves("6", 9999), (6.0, "given"))
        self.assertEqual(rpt._sanitize_serves(4.4, 1800), (4.0, "given"))  # rounded
        self.assertEqual(rpt._sanitize_serves(24, 1300), (24.0, "given"))  # "makes 24 cookies"

    def test_missing_or_implausible_is_estimated_from_weight(self):
        # ~450 g/serving, clamped to [1, 16]
        self.assertEqual(rpt._sanitize_serves(None, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(0, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(-3, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(200, 1800), (4.0, "estimated"))   # > 50 -> implausible
        # absurd total weight -> not trusted for the estimate -> fall back to 4
        self.assertEqual(rpt._sanitize_serves(None, 50000), (4.0, "estimated"))

    def test_no_signal_falls_back_to_one(self):
        self.assertEqual(rpt._sanitize_serves(None, 0), (1.0, "estimated"))


class WeightCapTests(unittest.TestCase):
    def test_normal_recipe_is_untouched(self):
        w, capped = rpt._cap_recipe_weights([250, 16, 490, 100, 28], 4)
        self.assertFalse(capped)
        self.assertEqual(w, [250.0, 16.0, 490.0, 100.0, 28.0])

    def test_dominant_inflated_ingredient_is_trimmed(self):
        # parse artefact: "313 cups flour" -> 39 kg, in a 4-serving recipe
        w, capped = rpt._cap_recipe_weights([125, 200, 39000, 80], 4)
        self.assertTrue(capped)
        self.assertLess(w[2], 5000)          # the 39 kg flour got trimmed
        self.assertEqual(w[0], 125.0)        # the others untouched
        self.assertLess(sum(w) / 4, 1500)    # recipe now < 1.5 kg/serving

    def test_uniformly_inflated_recipe_is_scaled_down(self):
        w, capped = rpt._cap_recipe_weights([5000, 4000, 3000], 4)
        self.assertTrue(capped)
        self.assertAlmostEqual(sum(w), 4 * 2500.0, delta=1.0)   # scaled to the ceiling
        # ratios preserved
        self.assertAlmostEqual(w[0] / w[1], 5000 / 4000, places=4)


def _scand(ingredient_name, distance, cf=1.0):
    return {"document": ingredient_name, "metadata": {"ingredient": ingredient_name, "cf_val": cf}, "distance": distance}


class SustainabilityMatchTests(unittest.TestCase):
    def test_exact_cf_index_lookup_wins(self):
        with patch.object(sc, "_cf_index", return_value={"beef": 19.5, "chicken": 6.0}):
            cf, name, conf = sc.best_sustainability_match("ground beef")  # "ground" stripped -> "beef"
        self.assertEqual(cf, 19.5)
        self.assertEqual(conf, "exact")

    def test_alias_to_db_entry(self):
        with patch.object(sc, "_cf_index", return_value={"flour": 1.4}):
            cf, name, conf = sc.best_sustainability_match("all-purpose flour")  # alias -> "flour"
        self.assertEqual(cf, 1.4)
        self.assertEqual(conf, "alias")

    def test_vector_path_food_class_guard(self):
        # query "chicken breast" (animal protein): skip the closer-but-incompatible
        # "wheat flour" candidate, take the compatible "chicken meat" one.
        with patch.object(sc, "_cf_index", return_value={}), \
             patch.object(sc, "query_sustainability_db",
                          return_value=[_scand("wheat flour", 0.10, 1.4), _scand("chicken meat", 0.25, 5.0)]):
            cf, name, conf = sc.best_sustainability_match("chicken breast fillet")
        self.assertEqual(name, "chicken meat")
        self.assertEqual(cf, 5.0)
        self.assertIn(conf, {"strong", "weak"})

    def test_vector_path_none_when_no_compatible(self):
        with patch.object(sc, "_cf_index", return_value={}), \
             patch.object(sc, "query_sustainability_db", return_value=[_scand("wheat flour", 0.10, 1.4)]):
            cf, name, conf = sc.best_sustainability_match("chicken breast")
        self.assertIsNone(cf)
        self.assertEqual(conf, "none")

    def test_survives_empty_db(self):
        with patch.object(sc, "_cf_index", return_value={}), \
             patch.object(sc, "query_sustainability_db", return_value=[]):
            cf, name, conf = sc.best_sustainability_match("Boneless, skinless chicken breast (about 1 lb)")
        self.assertIsNone(cf)
        self.assertEqual(conf, "none")


if __name__ == "__main__":
    unittest.main()
