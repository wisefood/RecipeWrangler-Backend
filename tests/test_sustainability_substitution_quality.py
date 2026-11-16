"""Round 2 rows 14 and 15: a CO2e swap must be judged on nutrition too.

The nutri-guard only rejects a candidate that costs the recipe a whole
Nutri-Score letter. Condensed milk for milk keeps the letter and still adds
~24 g of sugar a serving, so the judge has to see the per-nutrient change --
before this it saw only CO2e and had no way to object.
"""

import unittest
from unittest.mock import patch

from recipe_wrangler.services.adaptation.llm_judge import _build_prompt, _format_deltas
from recipe_wrangler.services.adaptation import service


def _sustainability_candidate(**overrides):
    candidate = {
        "rank": 1,
        "substitute_name": "condensed milk",
        "source": "graph",
        "original_cf_kg_co2e_per_kg": 3.2,
        "candidate_cf_kg_co2e_per_kg": 1.1,
        "co2e_reduction_per_serving_kg": 0.105,
        "co2e_reduction_pct": 0.65,
        "introduces_allergen": False,
        "new_allergens": [],
        "nutrient_delta_per_serving": {
            "energy_kcal": 118.0,
            "sugar_g": 24.5,
            "protein_g": 1.2,
            "fibre_g": 0.0,
        },
    }
    candidate.update(overrides)
    return candidate


def _prompt(candidates):
    return _build_prompt(
        recipe_title="Creamy tomato soup",
        recipe_ingredients=[{"ingredient": "milk", "weight_g": 200.0}],
        target_nutrient_label=None,
        target_points=None,
        offending_ingredient="milk",
        offending_pct=42.0,
        candidates=candidates,
        mode="sustainability",
    )


class SustainabilityPromptTests(unittest.TestCase):
    def test_sustainability_prompt_shows_the_per_serving_nutrition_change(self):
        prompt = _prompt([_sustainability_candidate()])
        self.assertIn("per-serving nutrition change:", prompt)
        self.assertIn("sugar_g: +24.5", prompt)

    def test_unmatched_candidate_says_so_rather_than_implying_no_change(self):
        prompt = _prompt([_sustainability_candidate(nutrient_delta_per_serving=None)])
        self.assertIn("no composition match", prompt)
        self.assertNotIn("sugar_g", prompt)

    def test_prompt_rejects_concentrated_and_vague_substitutes(self):
        prompt = _prompt([_sustainability_candidate()])
        # Row 14: condensed milk for milk.
        self.assertIn("condensed milk for milk", prompt)
        # Row 15: "milk drink" is a category, not something a shop sells.
        self.assertIn("milk drink", prompt)
        self.assertIn("too vague to shop for", prompt)

    def test_negligible_deltas_are_dropped(self):
        self.assertEqual(_format_deltas({"sugar_g": 0.01, "fat_g": -0.02}), "")
        self.assertEqual(_format_deltas(None), "")
        self.assertEqual(_format_deltas({"sugar_g": 24.5}), "sugar_g: +24.5")


class SustainabilityCandidateDeltaTests(unittest.TestCase):
    def _evaluate(self, cand_per_100g, grade="Nutriscore_C"):
        offender = {
            "name": "milk",
            "graph_name": "milk",
            "weight_g": 200.0,
            "cf_val": 3.2,
            "co2e_kg": 0.64,
            "contribution_pct": 0.42,
            "total_co2e_kg": 1.5,
            "detail": {
                "ingredient": "milk",
                "weight_g": 200.0,
                "co2e_kg": 0.64,
                "energy_kcal": 94.0,
                "sugar_g": 10.0,
                "protein_g": 6.8,
                "fibre_g": 0.0,
            },
        }
        details = [offender["detail"], {"ingredient": "tomato", "weight_g": 400.0, "co2e_kg": 0.86}]
        with patch.object(service, "_food_class_compatible", return_value=True), \
             patch.object(service, "best_sustainability_match", return_value=(1.1, None, None)), \
             patch.object(service, "_fetch_candidate_profile", return_value={"name": "condensed milk"}), \
             patch.object(service, "_candidate_per_100g_map", return_value=cand_per_100g), \
             patch.object(service, "_recipe_per_100g", return_value=({}, {}, 600.0)), \
             patch.object(service, "compute_nutri_score_breakdown_from_values",
                          return_value={"nutri_score": grade}), \
             patch.object(service, "get_ingredient_allergens", return_value=[]):
            return service._evaluate_sustainability_candidate(
                {"name": "condensed milk", "source": "graph", "category_distance": 1},
                offender,
                details,
                serves=4.0,
                original_allergens=set(),
                source="planeat_eu",
                fvl_pct=10.0,
                current_grade="C",
            )

    def test_condensed_milk_carries_its_sugar_cost_to_the_judge(self):
        result = self._evaluate({
            "energy_kcal_per_100g": 321.0,
            "sugars_per_100g": 54.0,
            "protein_per_100g": 7.9,
            "fibre_per_100g": 0.0,
        })
        self.assertIsNotNone(result)
        deltas = result["delta_per_serving"]
        # (200g/100 * 54g) = 108g sugar total, was 10g, over 4 servings.
        self.assertAlmostEqual(deltas["sugar_g"], 24.5, places=1)
        self.assertGreater(deltas["energy_kcal"], 0.0)

    def test_candidate_without_a_composition_match_reports_none_not_zero(self):
        with patch.object(service, "_food_class_compatible", return_value=True), \
             patch.object(service, "best_sustainability_match", return_value=(1.1, None, None)), \
             patch.object(service, "_fetch_candidate_profile", return_value=None), \
             patch.object(service, "get_ingredient_allergens", return_value=[]):
            result = service._evaluate_sustainability_candidate(
                {"name": "milk drink", "source": "graph", "category_distance": 1},
                {
                    "name": "milk", "graph_name": "milk", "weight_g": 200.0,
                    "cf_val": 3.2, "co2e_kg": 0.64, "contribution_pct": 0.42,
                    "total_co2e_kg": 1.5, "detail": {"ingredient": "milk", "weight_g": 200.0, "co2e_kg": 0.64},
                },
                [{"ingredient": "milk", "weight_g": 200.0, "co2e_kg": 0.64}],
                serves=4.0,
                original_allergens=set(),
                source="planeat_eu",
                fvl_pct=10.0,
                current_grade="C",
            )
        self.assertIsNotNone(result)
        self.assertIsNone(result["delta_per_serving"])


if __name__ == "__main__":
    unittest.main()
