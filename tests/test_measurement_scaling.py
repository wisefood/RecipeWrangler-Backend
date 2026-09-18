"""Round 2 row 10: rewriting a measurement for a different serving count."""

import unittest

from recipe_wrangler.api.routers.recipes import recipe_scale
from recipe_wrangler.schemas import RecipeScaleRequest
from recipe_wrangler.utils.measurement_scaling import scale_measurement


class WholeNumberTests(unittest.TestCase):
    def test_doubles_and_halves(self):
        self.assertEqual(scale_measurement("2 cups", 2), "4 cups")
        self.assertEqual(scale_measurement("4 cloves", 0.5), "2 cloves")

    def test_a_factor_of_one_is_returned_untouched(self):
        self.assertEqual(scale_measurement("1 1/2 cups", 1), "1 1/2 cups")


class FractionTests(unittest.TestCase):
    def test_mixed_numbers_survive_the_round_trip(self):
        self.assertEqual(scale_measurement("1 1/2 cups", 2), "3 cups")
        self.assertEqual(scale_measurement("1 1/2 cups", 0.5), "3/4 cup")

    def test_a_bare_fraction_scales_up_into_a_mixed_number(self):
        self.assertEqual(scale_measurement("1/2 cup", 3), "1 1/2 cups")

    def test_unicode_fractions_are_understood(self):
        self.assertEqual(scale_measurement("½ cup", 2), "1 cup")
        self.assertEqual(scale_measurement("¼ tsp", 4), "1 tsp")

    def test_a_unicode_fraction_glued_to_a_whole_number(self):
        # "1½ cups" is one and a half, not one times a half.
        self.assertEqual(scale_measurement("1½ cups", 2), "3 cups")

    def test_spoons_stay_in_kitchen_fractions(self):
        self.assertEqual(scale_measurement("1 tsp", 0.25), "1/4 tsp")
        self.assertEqual(scale_measurement("1 tbsp", 0.5), "1/2 tbsp")

    def test_something_smaller_than_an_eighth_is_not_rounded_away(self):
        # An eighth of a teaspoon halved is real; "0" would delete it.
        self.assertNotEqual(scale_measurement("1/8 tsp", 0.25), "0 tsp")


class DecimalUnitTests(unittest.TestCase):
    def test_metric_masses_stay_decimal(self):
        self.assertEqual(scale_measurement("200 g", 1.5), "300 g")
        self.assertEqual(scale_measurement("200 g", 0.5), "100 g")

    def test_grams_are_not_written_as_fractions(self):
        # "333 1/3 g" is not how anyone weighs anything.
        self.assertEqual(scale_measurement("500 g", 1 / 3), "167 g")

    def test_small_amounts_keep_a_decimal_place(self):
        self.assertEqual(scale_measurement("5 g", 1.5), "7.5 g")

    def test_comma_decimals_are_understood(self):
        self.assertEqual(scale_measurement("0,5 l", 2), "1 l")


class UnitAgreementTests(unittest.TestCase):
    def test_the_unit_agrees_with_the_new_number(self):
        self.assertEqual(scale_measurement("1 cup", 2), "2 cups")
        self.assertEqual(scale_measurement("2 cups", 0.5), "1 cup")
        self.assertEqual(scale_measurement("1 tablespoon", 4), "4 tablespoons")

    def test_capitalisation_is_preserved(self):
        self.assertEqual(scale_measurement("1 Clove", 2), "2 Cloves")

    def test_abbreviations_do_not_inflect(self):
        self.assertEqual(scale_measurement("1 tbsp", 2), "2 tbsp")
        self.assertEqual(scale_measurement("1 g", 2), "2 g")


class CompoundFormTests(unittest.TestCase):
    def test_a_multiplier_scales_the_count_not_the_pack(self):
        # Four 120 g tins, not two 240 g ones.
        self.assertEqual(
            scale_measurement("2 x 120g salmon fillets", 2), "4 x 120g salmon fillets"
        )

    def test_a_range_scales_both_ends(self):
        self.assertEqual(scale_measurement("2-3 tbsp", 2), "4-6 tbsp")

    def test_a_worded_range_keeps_its_wording(self):
        self.assertEqual(scale_measurement("1 to 2 cups", 2), "2 to 4 cups")

    def test_a_trailing_note_is_carried_through(self):
        self.assertEqual(
            scale_measurement("2 cloves garlic, crushed", 2), "4 cloves garlic, crushed"
        )


class UnparseableTests(unittest.TestCase):
    def test_measurements_with_no_quantity_are_returned_untouched(self):
        for text in ("salt to taste", "a pinch", "to serve", ""):
            with self.subTest(text=text):
                self.assertEqual(scale_measurement(text, 2), text)

    def test_a_nonsense_factor_changes_nothing(self):
        # Better an unscaled quantity than a wrong one.
        for factor in (0, -1, None, "two"):
            with self.subTest(factor=factor):
                self.assertEqual(scale_measurement("2 cups", factor), "2 cups")

    def test_a_zero_denominator_does_not_raise(self):
        self.assertEqual(scale_measurement("1/0 cup", 2), "1/0 cup")

    def test_none_is_tolerated(self):
        self.assertEqual(scale_measurement(None, 2), "")


class ScaleEndpointTests(unittest.TestCase):
    def _scale(self, measurements, from_serves, to_serves):
        return recipe_scale(
            RecipeScaleRequest(
                measurements=measurements,
                from_serves=from_serves,
                to_serves=to_serves,
            )
        )

    def test_scales_every_measurement_and_reports_the_factor(self):
        result = self._scale(["2 cups", "1 tbsp", "200 g"], 4, 6)
        self.assertEqual(result.factor, 1.5)
        self.assertEqual(result.measurements, ["3 cups", "1 1/2 tbsp", "300 g"])

    def test_the_response_stays_index_aligned_with_the_request(self):
        # The caller pairs these back up with ingredient names by position, so
        # an unparseable entry has to hold its place rather than be dropped.
        measurements = ["2 cups", "salt to taste", "1 tbsp"]
        result = self._scale(measurements, 4, 8)
        self.assertEqual(len(result.measurements), len(measurements))
        self.assertEqual(result.measurements[1], "salt to taste")

    def test_scaling_down_works_too(self):
        result = self._scale(["4 cloves", "2 cups"], 8, 4)
        self.assertEqual(result.measurements, ["2 cloves", "1 cup"])

    def test_the_same_serving_count_is_a_no_op(self):
        result = self._scale(["1 1/2 cups"], 4, 4)
        self.assertEqual(result.factor, 1.0)
        self.assertEqual(result.measurements, ["1 1/2 cups"])

    def test_an_empty_list_is_accepted(self):
        self.assertEqual(self._scale([], 4, 6).measurements, [])

    def test_a_non_positive_serving_count_is_rejected_by_the_schema(self):
        from pydantic import ValidationError

        for from_serves, to_serves in ((0, 4), (4, 0), (-1, 4)):
            with self.subTest(from_serves=from_serves, to_serves=to_serves):
                with self.assertRaises(ValidationError):
                    RecipeScaleRequest(
                        measurements=["2 cups"],
                        from_serves=from_serves,
                        to_serves=to_serves,
                    )


if __name__ == "__main__":
    unittest.main()
