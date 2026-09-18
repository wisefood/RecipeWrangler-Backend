"""Round 2 row 9: "per serving" has to say what a serving is."""

import unittest

from recipe_wrangler.api.routers.recipes import _serving_weight_g


class ServingWeightTests(unittest.TestCase):
    def test_divides_total_ingredient_weight_by_servings(self):
        details = [
            {"name": "potato", "weight_g": 600.0},
            {"name": "leek", "weight_g": 300.0},
            {"name": "butter", "weight_g": 30.0},
        ]
        self.assertAlmostEqual(_serving_weight_g(details, 4), 232.5)

    def test_a_zero_weight_ingredient_suppresses_the_answer(self):
        # The catalogue's zero-weight corruption would otherwise publish a
        # serving weight that quietly omits the ingredients it failed to weigh.
        details = [
            {"name": "potato", "weight_g": 600.0},
            {"name": "black pepper", "weight_g": 0.0},
        ]
        self.assertIsNone(_serving_weight_g(details, 4))

    def test_a_missing_weight_suppresses_the_answer(self):
        details = [{"name": "potato", "weight_g": 600.0}, {"name": "leek"}]
        self.assertIsNone(_serving_weight_g(details, 4))

    def test_unknown_or_nonsense_serving_count_returns_none(self):
        details = [{"name": "potato", "weight_g": 600.0}]
        self.assertIsNone(_serving_weight_g(details, None))
        self.assertIsNone(_serving_weight_g(details, 0))
        self.assertIsNone(_serving_weight_g(details, -2))

    def test_no_profile_returns_none(self):
        self.assertIsNone(_serving_weight_g(None, 4))
        self.assertIsNone(_serving_weight_g([], 4))

    def test_string_weights_and_serves_are_coerced(self):
        details = [{"name": "potato", "weight_g": "600"}, {"name": "leek", "weight_g": "300"}]
        self.assertAlmostEqual(_serving_weight_g(details, "3"), 300.0)


if __name__ == "__main__":
    unittest.main()
