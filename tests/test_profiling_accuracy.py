import unittest
from unittest.mock import Mock, patch

from recipe_wrangler.tools import recipe_profiling_tool as rpt
from recipe_wrangler.tools import sustainability_calculator as sc


class ServesSanitizeTests(unittest.TestCase):
    def test_plausible_value_is_kept(self):
        self.assertEqual(rpt._sanitize_serves(4, 1800), (4.0, "given"))
        self.assertEqual(rpt._sanitize_serves("6", 9999), (6.0, "given"))
        self.assertEqual(rpt._sanitize_serves(4.4, 1800), (4.0, "given"))  # rounded
        self.assertEqual(rpt._sanitize_serves(24, 1300), (24.0, "given"))  # "makes 24 cookies"
        self.assertEqual(rpt._sanitize_serves(100, 5000), (100.0, "given"))  # syrup servings

    def test_missing_or_implausible_is_estimated_from_weight(self):
        # ~450 g/serving, clamped to [1, 16]
        self.assertEqual(rpt._sanitize_serves(None, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(0, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(-3, 1800), (4.0, "estimated"))
        self.assertEqual(rpt._sanitize_serves(501, 1800), (4.0, "estimated"))
        # absurd total weight -> not trusted for the estimate -> fall back to 4
        self.assertEqual(rpt._sanitize_serves(None, 50000), (4.0, "estimated"))

    def test_no_signal_falls_back_to_one(self):
        self.assertEqual(rpt._sanitize_serves(None, 0), (1.0, "estimated"))


class WeightCapTests(unittest.TestCase):
    def test_explicit_edible_grams_per_person_override_carcass_weight(self):
        weights, changed = rpt._apply_explicit_per_person_weights(
            ["whole chicken (100g cooked chicken per person)", "potatoes"],
            [1500.0, 250.0],
            2,
        )
        self.assertEqual(weights, [200.0, 250.0])
        self.assertEqual(changed, [0])

    def test_per_person_override_ignores_unrelated_gram_text(self):
        weights, changed = rpt._apply_explicit_per_person_weights(
            ["whole 1.5kg chicken", "100g potatoes"], [1500.0, 100.0], 2
        )
        self.assertEqual(weights, [1500.0, 100.0])
        self.assertEqual(changed, [])

    def test_explicit_served_meat_total_overrides_reserved_whole_bird(self):
        weights, changed = rpt._apply_explicit_served_meat_weight(
            ["chicken (see tip)", "potatoes"],
            [
                "Serve 2 portions (300g cooked meat) of the chicken with "
                "potatoes. Reserve remaining chicken for other dishes."
            ],
            [1300.0, 300.0],
        )
        self.assertEqual(weights, [300.0, 300.0])
        self.assertEqual(changed, [0])

    def test_served_meat_override_requires_unambiguous_source_instruction(self):
        weights, changed = rpt._apply_explicit_served_meat_weight(
            ["chicken", "potatoes"],
            ["Cook until the chicken reaches 75C."],
            [1300.0, 300.0],
        )
        self.assertEqual(weights, [1300.0, 300.0])
        self.assertEqual(changed, [])

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
        self.assertAlmostEqual(sum(w), 4 * 700.0, delta=1.0)   # scaled to the sane target
        # ratios preserved
        self.assertAlmostEqual(w[0] / w[1], 5000 / 4000, places=4)

    def test_plain_salad_cup_uses_leaf_density(self):
        from recipe_wrangler.tools.ingredient_weight_tool import _common_unit_reference_grams

        self.assertEqual(
            _common_unit_reference_grams("mixed salad leaves", "cup"),
            (40.0, "loose mixed salad cup"),
        )


class MatchQueryPropagationTests(unittest.TestCase):
    def test_matching_uses_full_names_but_output_keeps_display_names(self):
        nutrition = {
            "source_key": "eu",
            "source": "EU Composite",
            "details": [
                {
                    "ingredient": "cooked green lentils",
                    "matched_nutritional_ingredient": "Lentil, green, cooked",
                }
            ],
        }
        sustainability = {
            "details": [
                {
                    "ingredient": "cooked green lentils",
                    "matched_sustainability_ingredient": "lentils",
                    "cf_val": 1.0,
                }
            ],
            "serves": 2,
        }

        nutrition_tool = Mock()
        nutrition_tool.invoke.return_value = nutrition
        sustainability_tool = Mock()
        sustainability_tool.invoke.return_value = sustainability
        with patch.object(
            rpt,
            "nutritional_tool_vector",
            nutrition_tool,
        ), patch.object(
            rpt,
            "sustainability_tool_vector",
            sustainability_tool,
        ):
            result = rpt.Recipe_Profiling_Tool(
                {
                    "title": "Test",
                    "ingredient_names": ["lentils"],
                    "ingredient_match_names": ["cooked green lentils"],
                    "measurements": ["200 g"],
                    "weights": [200.0],
                    "serves": 2,
                    "region": "EU",
                }
            )

        assert nutrition_tool.invoke.call_args.args[0]["ingredient_names"] == [
            "cooked green lentils"
        ]
        assert sustainability_tool.invoke.call_args.args[0]["ingredient_names"] == [
            "cooked green lentils"
        ]
        assert result["ingredients"][0]["ingredient"] == "lentils"
        assert result["sustainability_details"][0]["ingredient"] == "lentils"


def _scand(ingredient_name, distance, cf=1.0):
    return {"document": ingredient_name, "metadata": {"ingredient": ingredient_name, "cf_val": cf}, "distance": distance}


class SustainabilityMatchTests(unittest.TestCase):
    """Reviewed AGRIBALYSE 3.2 name mapping (sustainability_factors); values are the no-preparation scenario, kg CO2e/kg."""

    def test_meat_names_map_to_their_own_records(self):
        beef, _, conf = sc.best_sustainability_match("minced beef")
        chicken, _, _ = sc.best_sustainability_match("chicken breast fillets")
        self.assertEqual(conf, "source_reviewed")
        self.assertGreater(beef, 5 * chicken)  # beef is the high-impact meat; not one value for every meat

    def test_stock_is_not_the_meat(self):
        stock, _, _ = sc.best_sustainability_match("beef stock cube")
        beef, _, _ = sc.best_sustainability_match("beef")
        self.assertLess(stock, beef)

    def test_composite_and_unknown_names_are_unscored(self):
        for name in ("pizza dough", "zzzz unknown thing"):
            cf, _, conf = sc.best_sustainability_match(name)
            self.assertIsNone(cf)
            self.assertEqual(conf, "none")

    def test_concito_fallback_only_for_listed_foods(self):
        cf, _, conf = sc.best_sustainability_match("button mushrooms", region="IE")
        self.assertIsNotNone(cf)
        self.assertIn(conf, {"source_reviewed", "fallback"})


if __name__ == "__main__":
    unittest.main()
