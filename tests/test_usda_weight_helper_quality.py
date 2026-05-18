import json
import tempfile
import unittest
from pathlib import Path

from recipe_wrangler.utils import weigh_calculation_usda_ as weights


class USDAWeightHelperQualityTests(unittest.TestCase):
    def write_weights(self, rows):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with tmp:
            json.dump(rows, tmp)
        return Path(tmp.name)

    def test_name_fallback_skips_unlinked_rows_by_default(self):
        path = self.write_weights(
            [
                {
                    "usda_id": None,
                    "food_name": "Gefilte fish",
                    "portions": [
                        {
                            "amount": 5.0,
                            "portion_desc": "balls",
                            "grams": 41.0,
                            "grams_per_unit": 41.0,
                        }
                    ],
                }
            ]
        )

        self.assertIsNone(weights.find_weight_match_by_name("Gefilte fish", "balls", path))

        match = weights.find_weight_match_by_name(
            "Gefilte fish",
            "balls",
            path,
            allow_unlinked=True,
        )
        self.assertIsNotNone(match)
        self.assertIsNone(match["usda_id"])
        self.assertAlmostEqual(match["portion"]["grams_per_unit"], 8.2)

    def test_portion_grams_per_unit_is_recomputed_from_amount(self):
        path = self.write_weights(
            [
                {
                    "usda_id": "05140",
                    "food_name": "Duck, cooked",
                    "portions": [
                        {
                            "amount": 0.5,
                            "portion_desc": "duck",
                            "grams": 382.0,
                            "grams_per_unit": 382.0,
                        }
                    ],
                }
            ]
        )

        self.assertAlmostEqual(weights.grams_for_food_id("05140", "duck", 1, path), 764.0)

    def test_ambiguous_yield_portions_are_ignored(self):
        path = self.write_weights(
            [
                {
                    "usda_id": "08000",
                    "food_name": "Grits, dry",
                    "portions": [
                        {
                            "amount": 1.0,
                            "portion_desc": "cup, dry, yields",
                            "grams": 965.0,
                            "grams_per_unit": 965.0,
                        }
                    ],
                }
            ]
        )

        self.assertIsNone(weights.match_portion("08000", "cup", weights_path=path))
        self.assertIsNone(weights.grams_for_food_id("08000", "cup", 1, path))

    def test_non_dry_yield_portions_are_ignored(self):
        path = self.write_weights(
            [
                {
                    "usda_id": "19175",
                    "food_name": "Gelatin dessert powder",
                    "portions": [
                        {
                            "amount": 1.0,
                            "portion_desc": "package 3 oz, yields 2 cups",
                            "grams": 540.0,
                            "grams_per_unit": 540.0,
                        }
                    ],
                }
            ]
        )

        self.assertIsNone(weights.match_portion("19175", "package", weights_path=path))
        self.assertIsNone(weights.grams_for_food_id("19175", "package", 1, path))


if __name__ == "__main__":
    unittest.main()
