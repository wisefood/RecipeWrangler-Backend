import unittest

from recipe_wrangler.tools.recipe_profiling_chain import split_ingredient_lines


class IngredientLineSplitterTests(unittest.TestCase):
    def test_unicode_mixed_fraction_line(self):
        names, measurements = split_ingredient_lines(["2 ¼ cups frozen sweet corn"])

        self.assertEqual(names, ["frozen sweet corn"])
        self.assertEqual(measurements, ["2 ¼ cups"])

    def test_package_after_mass_line_drops_container_from_name(self):
        names, measurements = split_ingredient_lines(["300g can creamed corn"])

        self.assertEqual(names, ["creamed corn"])
        self.assertEqual(measurements, ["300 g"])

    def test_multiplier_mass_line_becomes_total_mass(self):
        names, measurements = split_ingredient_lines(["2 x 120g salmon fillets"])

        self.assertEqual(names, ["salmon fillets"])
        self.assertEqual(measurements, ["240 g"])

    def test_concatenated_source_line_splits_before_next_quantity(self):
        names, measurements = split_ingredient_lines(
            ["1 teaspoon vegetable oil2 Tablespoons lemon juice"]
        )

        self.assertEqual(names, ["vegetable oil", "lemon juice"])
        self.assertEqual(measurements, ["1 teaspoon", "2 Tablespoons"])

    def test_sampled_count_units_and_multiword_fluid_ounces(self):
        names, measurements = split_ingredient_lines(
            [
                "18 cubes watermelon, cut into 18 2cm cubes",
                "4 nori sheets",
                "46 us fluid ounces tomato juice, low-sodium",
            ]
        )

        self.assertEqual(
            names,
            [
                "watermelon, cut into 18 2cm cubes",
                "nori sheets",
                "tomato juice, low-sodium",
            ],
        )
        self.assertEqual(measurements, ["18 cubes", "4", "46 us fluid ounces"])

    def test_repairs_recipe1m_slashless_volume_fractions(self):
        names, measurements = split_ingredient_lines(
            [
                "34 cup sugar",
                "1 12 teaspoons chopped pickled ginger",
                "18 cups Flour",
            ]
        )

        self.assertEqual(names, ["sugar", "chopped pickled ginger", "Flour"])
        self.assertEqual(measurements, ["3/4 cup", "1 1/2 teaspoons", "1/8 cups"])

    def test_repairs_recipe1m_slashless_mass_fractions(self):
        names, measurements = split_ingredient_lines(
            [
                "34 lb pork sausage",
                "12 lbs Velveeta cheese",
                "12 lb spaghettini",
                "1 12 pounds chicken thighs",
            ]
        )

        self.assertEqual(
            names,
            [
                "pork sausage",
                "Velveeta cheese",
                "spaghettini",
                "chicken thighs",
            ],
        )
        self.assertEqual(
            measurements,
            ["3/4 lb", "1/2 lbs", "1/2 lb", "1 1/2 pounds"],
        )

    def test_repairs_slashless_range_fractions(self):
        names, measurements = split_ingredient_lines(
            [
                "14-1 cup butter",
                "14-13 cup margarine",
                "12-1 pound chicken thighs",
            ]
        )

        self.assertEqual(
            names,
            ["butter", "margarine", "chicken thighs"],
        )
        self.assertEqual(
            measurements,
            ["1/4-1 cup", "1/4-1/3 cup", "1/2-1 pound"],
        )

    def test_does_not_corrupt_real_oz_can_sizes(self):
        # "12 oz", "14 oz", "16 oz" are real can sizes — they must survive.
        names, measurements = split_ingredient_lines(
            [
                "12 oz can crushed tomatoes",
                "14 oz package frozen spinach",
            ]
        )

        self.assertEqual(names, ["crushed tomatoes", "frozen spinach"])
        self.assertEqual(measurements, ["12 oz", "14 oz"])


if __name__ == "__main__":
    unittest.main()
