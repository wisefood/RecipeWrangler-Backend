import unittest
from unittest.mock import patch

from recipe_wrangler.tools import ingredient_weight_tool as mod


class IngredientWeightConfidenceTests(unittest.TestCase):
    def setUp(self):
        mod._embedding_usda_link.cache_clear()
        mod._usda_links_lexical_index.cache_clear()
        mod._foodon_class_ids_for_ingredient.cache_clear()
        mod._foodon_classes_have_common_ancestor.cache_clear()

    def invoke_debug(self, names, measurements):
        return mod.ingredient_weight_tool_usda.invoke(
            {
                "ingredient_names": names,
                "measurements": measurements,
                "return_details": True,
                "debug": True,
            }
        )

    def test_to_taste_zero_does_not_call_live_llm(self):
        with patch.object(mod, "_live_llm_weight_fallback", side_effect=AssertionError):
            result = self.invoke_debug(["salt"], ["to taste"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [0.0])
        self.assertEqual(detail["match_type"], "to_taste_zero")
        self.assertEqual(detail["confidence"], 0.9)

    def test_liquid_density_for_olive_oil_ml(self):
        with patch.object(mod, "canonical_name_to_usda", return_value={"usda_id": "oil", "canonical": "olive oil"}):
            result = self.invoke_debug(["olive oil"], ["100ml"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 92.0)
        self.assertEqual(detail["match_type"], "liquid_density_volume_fallback")
        self.assertEqual(detail["confidence"], 0.65)

    def test_informal_splash_uses_liquid_density(self):
        with patch.object(mod, "canonical_name_to_usda", return_value={"usda_id": "milk", "canonical": "milk"}):
            result = self.invoke_debug(["milk"], ["a splash"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 15.45)
        self.assertEqual(detail["parsed_unit"], "splash")
        self.assertEqual(detail["match_type"], "liquid_density_volume_fallback")

    def test_nut_seed_butter_volume_uses_canonical_density(self):
        with patch.object(
            mod,
            "canonical_name_to_usda",
            return_value={
                "usda_id": "tahini",
                "canonical": "Seeds, sesame butter, tahini",
                "food_group": "Nut and Seed Products",
            },
        ):
            result = self.invoke_debug(["tahini"], ["50ml"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 55.0)
        self.assertEqual(detail["match_type"], "liquid_density_volume_fallback")
        self.assertFalse(detail.get("live_llm_fallback"))

    def test_missing_quantity_countable_ingredient_infers_one_unit(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value={"usda_id": "egg", "canonical": "egg"}),
            patch.object(mod, "_estimate_grams_from_usda_id", return_value=(50.0, {"portion_desc": "egg"}, "direct")),
        ):
            result = self.invoke_debug(["egg"], [None])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [50.0])
        self.assertEqual(detail["parsed_quantity"], "1")
        self.assertEqual(detail["parsed_unit"], "egg")
        self.assertTrue(detail["quantity_inferred"])
        self.assertTrue(detail["unit_inferred"])

    def test_large_bare_number_is_inferred_as_grams_not_countable_unit(self):
        with patch.object(
            mod,
            "canonical_name_to_usda",
            return_value={
                "usda_id": "07000",
                "canonical": "Pork sausage",
                "food_group": "Sausages and Luncheon Meats",
            },
        ):
            result = self.invoke_debug(["pork sausage"], ["750"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [750.0])
        self.assertEqual(detail["parsed_unit"], "gram")
        self.assertTrue(detail["unit_inferred"])
        self.assertEqual(detail["match_type"], "direct_mass")

    def test_qualifier_stripping(self):
        self.assertEqual(mod._strip_qualifiers("extra virgin olive oil"), "olive oil")
        self.assertEqual(mod._strip_qualifiers("freshly ground black pepper"), "black pepper")

    def test_split_mixed_number_parses_as_mixed_fraction_not_range(self):
        qty, unit, inferred = mod._split_measurement("1- 1/2 cup")

        self.assertEqual(qty, "1 1/2")
        self.assertEqual(unit, "cup")
        self.assertFalse(inferred)
        self.assertEqual(mod._parse_quantity_value(qty), 1.5)

    def test_unicode_mixed_fraction_and_multiplier_mass_parse(self):
        qty, unit, inferred = mod._split_measurement("2 ¼ cups")
        self.assertEqual(qty, "2 1/4")
        self.assertEqual(unit, "cup")
        self.assertFalse(inferred)
        self.assertEqual(mod._parse_quantity_value(qty), 2.25)

        qty, unit, inferred = mod._split_measurement("2 x 120g")
        self.assertEqual(qty, "240.0")
        self.assertEqual(unit, "g")
        self.assertFalse(inferred)

    def test_slashless_mass_fractions_repair_pound_artifacts(self):
        for raw, expected_qty, expected_unit in (
            ("12 lb", "1/2", "lb"),
            ("34 lb", "3/4", "lb"),
            ("14 lbs", "1/4", "lbs"),
            ("12 pound", "1/2", "pound"),
            ("12 pounds", "1/2", "pounds"),
            ("1 12 lbs", "1 1/2", "lbs"),
        ):
            with self.subTest(raw=raw):
                qty, unit, _ = mod._split_measurement(raw)
                self.assertEqual(qty, expected_qty)
                self.assertEqual(unit, expected_unit)

    def test_slashless_range_fraction_repair_volume_and_mass(self):
        for raw, expected in (
            # fraction-int range
            ("14-1 cup", "1/4-1 cup"),
            ("34-1 cup", "3/4-1 cup"),
            # fraction-fraction range
            ("14-13 cup", "1/4-1/3 cup"),
            ("13-23 cup", "1/3-2/3 cup"),
            ("12-34 tablespoons", "1/2-3/4 tablespoons"),
            # mass
            ("14-12 lb", "1/4-1/2 lb"),
            ("12-1 pound", "1/2-1 pound"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    mod._repair_slashless_fraction_measurement(raw.lower()),
                    expected,
                )

    def test_slashless_range_then_average_via_split_measurement(self):
        # "14-13 cup" → "1/4-1/3 cup" → averaged to (0.25 + 0.333)/2 ≈ 0.292
        qty, unit, _ = mod._split_measurement("14-13 cup")
        self.assertEqual(unit, "cup")
        self.assertAlmostEqual(mod._parse_quantity_value(qty), (0.25 + 1/3) / 2, places=5)

    def test_slashless_mass_fractions_leave_real_oz_can_sizes_alone(self):
        # 12 oz / 14 oz / 16 oz are real can sizes — must not be repaired.
        for raw in ("12 oz", "14 oz", "16 oz", "12 g"):
            with self.subTest(raw=raw):
                qty, unit, _ = mod._split_measurement(raw)
                # We expect parsing to keep the integer quantity as-is.
                self.assertNotIn("/", qty or "")

    def test_bare_package_size_prefix_is_not_a_count_multiplier(self):
        # "28 oz can" means one 28-oz can, not 28 cans.
        for raw, container in (
            ("28 oz can", "can"),
            ("14.5 oz can", "can"),
            ("400 g tin", "tin"),
            ("15 oz can", "can"),
            ("6 oz jar", "jar"),
            ("250 ml carton", "carton"),
        ):
            with self.subTest(raw=raw):
                qty, unit, inferred = mod._split_measurement(raw)
                self.assertEqual(qty, "1")
                self.assertEqual(unit, container)
                self.assertTrue(inferred)

    def test_bare_oz_can_weight_matches_package_size_not_n_times_it(self):
        result = mod.ingredient_weight_tool_usda.invoke(
            {
                "ingredient_names": ["canned diced tomatoes"],
                "measurements": ["28 oz can"],
                "return_details": True,
                "debug": True,
            }
        )
        detail = result["details"][0]
        self.assertEqual(detail["match_type"], "explicit_package_size_fallback")
        # 28 oz ≈ 794 g — not 28 × 794.
        self.assertAlmostEqual(detail["weight_grams"], 28 * 28.349523125, delta=1.0)

    def test_extract_leading_measurement_from_name_lifts_fused_qty_unit(self):
        result = mod._extract_leading_measurement_from_name(
            "12 lbs lean hamburger, no more than 10% fat"
        )
        self.assertIsNotNone(result)
        qty, unit, rest = result
        self.assertEqual(qty, "1/2")
        self.assertEqual(unit, "lbs")
        self.assertEqual(rest, "lean hamburger, no more than 10% fat")

    def test_extract_leading_measurement_returns_none_for_plain_names(self):
        for name in (
            "olive oil",
            "scallions, sliced thinly",
            "1 1/2 cups",
        ):
            with self.subTest(name=name):
                self.assertIsNone(mod._extract_leading_measurement_from_name(name))

    def test_extract_leading_unit_from_name_lifts_fused_abbreviation(self):
        self.assertEqual(
            mod._extract_leading_unit_from_name("c. grated Parmesan cheese"),
            ("cup", "grated Parmesan cheese"),
        )
        self.assertEqual(mod._extract_leading_unit_from_name("dashes Salt"), ("dash", "Salt"))
        self.assertEqual(
            mod._extract_leading_unit_from_name("pkg. cream cheese"), ("package", "cream cheese")
        )
        self.assertEqual(
            mod._extract_leading_unit_from_name("slices of bread"), ("slice", "bread")
        )
        # Words that merely start with a unit substring must not be split.
        for name in ("candy bar", "canned tomatoes", "cinnamon", "olive oil", "c."):
            with self.subTest(name=name):
                self.assertIsNone(mod._extract_leading_unit_from_name(name))

    def test_weight_tool_recombines_leading_name_unit_with_measurement_quantity(self):
        result = mod.ingredient_weight_tool_usda.invoke(
            {
                "ingredient_names": ["c. lowfat milk", "dashes Salt"],
                "measurements": ["1/3", "2"],
                "return_details": True,
                "debug": True,
            }
        )
        d0, d1 = result["details"]
        self.assertEqual(d0["parsed_unit"], "cup")
        self.assertIsNone(d0["error"])
        self.assertGreater(d0["weight_grams"], 50)
        self.assertLess(d0["weight_grams"], 120)
        self.assertEqual(d1["parsed_unit"], "dash")
        self.assertIsNone(d1["error"])
        self.assertLess(d1["weight_grams"], 5)

    def test_offline_reference_short_circuits_cascade_when_present(self):
        index = {
            ("brown sugar", "cup"): {
                "ingredient": "brown sugar",
                "normalized_unit": "cup",
                "grams_per_unit": 220.0,
                "source_type": "accepted_deterministic",
                "confidence": 0.92,
                "reference_measurement": "1 cup",
            },
        }
        with (
            patch.object(mod, "OFFLINE_REFERENCE_DATASET_ENABLED", True),
            patch.object(mod, "_load_offline_reference_dataset_index", return_value=index),
            patch.object(mod, "canonical_name_to_usda", side_effect=AssertionError),
            patch.object(mod, "_embedding_usda_link", side_effect=AssertionError),
        ):
            result = self.invoke_debug(["brown sugar"], ["1/2 cup"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 110.0)
        self.assertEqual(detail["match_type"], mod.OFFLINE_REFERENCE_MATCH_TYPE)
        self.assertTrue(detail["offline_reference"])
        self.assertEqual(detail["offline_reference_source_type"], "accepted_deterministic")
        self.assertEqual(detail["confidence"], 0.90)

    def test_offline_reference_is_silent_no_op_when_disabled(self):
        with (
            patch.object(mod, "OFFLINE_REFERENCE_DATASET_ENABLED", False),
            patch.object(mod, "_load_offline_reference_dataset_index", side_effect=AssertionError),
        ):
            self.assertIsNone(mod._lookup_offline_reference("anything", "cup"))

    def test_offline_reference_skips_low_confidence_llm_rebuilt(self):
        with (
            patch.object(mod, "OFFLINE_REFERENCE_DATASET_ENABLED", True),
            patch.object(
                mod,
                "_csv_rows_from_path_or_pg",
                return_value=[
                    {
                        "ingredient": "obscure thing",
                        "normalized_unit": "cup",
                        "weight_grams": "100",
                        "source_type": "llm_rebuilt",
                        "confidence": "0.5",  # below 0.7 threshold
                        "reference_measurement": "1 cup",
                    },
                    {
                        "ingredient": "trusted thing",
                        "normalized_unit": "cup",
                        "weight_grams": "100",
                        "source_type": "llm_rebuilt",
                        "confidence": "0.85",
                        "reference_measurement": "1 cup",
                    },
                ],
            ),
        ):
            mod._load_offline_reference_dataset_index.cache_clear()
            self.assertIsNone(mod._lookup_offline_reference("obscure thing", "cup"))
            hit = mod._lookup_offline_reference("trusted thing", "cup")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["source_type"], "llm_rebuilt")
            mod._load_offline_reference_dataset_index.cache_clear()

    def test_weight_tool_uses_name_prefix_when_measurement_lacks_unit(self):
        with (
            patch.object(
                mod,
                "canonical_name_to_usda",
                return_value={
                    "usda_id": "23572",
                    "canonical": "Beef, ground",
                    "food_group": "Beef Products",
                },
            ),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_weight_name_usda_link", return_value=None),
            patch.object(mod, "_reference_weight_fallback", return_value=None),
        ):
            result = self.invoke_debug(
                ["12 lbs lean hamburger, no more than 10% fat"],
                ["1"],
            )

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 226.79618, places=3)
        self.assertEqual(detail["parsed_quantity"], "1/2")
        self.assertEqual(detail["parsed_unit"], "lbs")
        self.assertEqual(detail["match_type"], "direct_mass")

    def test_missing_scallions_are_not_treated_as_pinch(self):
        with (
            patch.object(
                mod,
                "canonical_name_to_usda",
                return_value={
                    "usda_id": "02044",
                    "canonical": "fresh scallions",
                    "food_group": mod.HERB_SPICE_FOOD_GROUP,
                },
            ),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["scallions"], [""])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [0.0])
        self.assertIsNone(detail["weight_grams"])
        self.assertEqual(detail["error"], "missing_quantity")
        self.assertNotEqual(detail["match_type"], "pinch_default_missing_quantity_fallback")

    def test_common_count_unit_references_cover_produce_and_herbs(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(
                ["fresh scallions", "spinach", "fresh parsley"],
                ["1 bunch", "1 bunch", "2 sprigs"],
            )

        self.assertEqual(result["weights"], [100.0, 250.0, 2.0])
        self.assertTrue(
            all(d["match_type"] == "common_unit_reference_fallback" for d in result["details"])
        )

    def test_common_kitchen_references_cover_countable_gaps(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(
                [
                    "butter",
                    "large egg",
                    "egg whites",
                    "fresh basil",
                    "black pepper",
                    "gelatin powder",
                    "ice cubes",
                ],
                [
                    "1 stick",
                    "2",
                    "4",
                    "10 leaves",
                    "1 pinch",
                    "1 packet",
                    "1 cup",
                ],
            )

        self.assertEqual(result["weights"], [113.0, 100.0, 132.0, 5.0, 0.3, 7.0, 140.0])
        self.assertTrue(
            all(d["match_type"] == "common_unit_reference_fallback" for d in result["details"])
        )

    def test_common_references_cover_sampled_countable_produce_and_protein(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(
                [
                    "apples",
                    "bananas",
                    "lemon",
                    "lime",
                    "bell pepper",
                    "medium potatoes",
                    "broccoli",
                    "cauliflower",
                    "salmon fillets",
                    "lamb chops",
                    "wholemeal burger buns",
                ],
                [
                    "6",
                    "2",
                    "1 whole",
                    "1/2 whole",
                    "1 whole",
                    "2 medium",
                    "3 stalks",
                    "1 large head",
                    "2 fillets",
                    "4 chops",
                    "4 buns",
                ],
            )

        self.assertEqual(
            result["weights"],
            [1092.0, 236.0, 58.0, 33.5, 120.0, 346.0, 453.0, 776.25, 240.0, 452.0, 240.0],
        )
        self.assertTrue(
            all(d["match_type"] == "common_unit_reference_fallback" for d in result["details"])
        )

    def test_common_references_cover_more_sampled_count_units(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(
                [
                    "shallots",
                    "peach",
                    "jalapeno chiles",
                    "nori sheets",
                    "celery stalks, chopped",
                    "baby fennel bulb",
                    "chicken breasts",
                    "raw prawns",
                    "English muffin",
                    "cheddar cheese",
                    "avocado, sliced",
                ],
                ["4", "1", "3", "4", "3", "1", "4", "12", "1", "2 slices", "1"],
            )

        self.assertEqual(
            result["weights"],
            [100.0, 150.0, 42.0, 10.0, 120.0, 234.0, 696.0, 240.0, 57.0, 40.0, 150.0],
        )
        self.assertTrue(
            all(d["match_type"] == "common_unit_reference_fallback" for d in result["details"])
        )

    def test_mass_units_convert_without_usda_match(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_weight_name_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["medium shrimp"], ["1 lb"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 453.59237)
        self.assertEqual(detail["match_type"], "direct_mass")
        self.assertIsNone(detail["error"])

    def test_dry_bouillon_powder_does_not_use_water_like_density(self):
        self.assertIsNone(mod._liquid_density_for_name("chicken bouillon powder"))

    def test_hard_cheese_volume_uses_common_reference(self):
        with (
            patch.object(
                mod,
                "canonical_name_to_usda",
                return_value={
                    "usda_id": "01270",
                    "canonical": "Cheese, cheddar, sharp",
                    "food_group": "Dairy and Egg Products",
                },
            ),
            patch.object(mod, "_embedding_usda_link", side_effect=AssertionError),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["sharp cheddar cheese"], ["2 cups"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [226.0])
        self.assertEqual(detail["match_type"], "common_unit_reference_fallback")
        self.assertEqual(detail["usda_id"], "01270")

    def test_generic_cached_llm_package_portions_require_explicit_size(self):
        with (
            patch.object(mod, "RECIPE1M_LLM_FALLBACK_ENABLED", True),
            patch.object(mod, "_load_recipe1m_llm_portion_fallback_index") as load_index,
        ):
            load_index.return_value = {
                "by_name_unit": {
                    ("cool whip", "package"): {
                        "ingredient": "Cool Whip",
                        "unit": "package",
                        "grams_per_unit": 340.0,
                        "sample_measurement": "1 package",
                        "usda_id": "04014",
                    },
                    ("cool whip 8 oz", "package"): {
                        "ingredient": "Cool Whip 8 oz",
                        "unit": "package",
                        "grams_per_unit": 226.0,
                        "sample_measurement": "1 8 oz package",
                        "usda_id": "04014",
                    },
                },
                "by_usda_unit": {},
            }

            self.assertIsNone(
                mod._lookup_recipe1m_llm_portion_fallback(
                    name="Cool Whip",
                    unit="package",
                    usda_id=None,
                    measurement="1 package",
                )
            )
            self.assertIsNotNone(
                mod._lookup_recipe1m_llm_portion_fallback(
                    name="Cool Whip 8 oz",
                    unit="package",
                    usda_id=None,
                    measurement="1 8 oz package",
                )
            )

    def test_explicit_package_size_is_used_before_generic_llm_package(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["Cool Whip"], ["1 8 oz package"])

        detail = result["details"][0]
        self.assertAlmostEqual(result["weights"][0], 226.796185)
        self.assertEqual(detail["parsed_unit"], "package")
        self.assertEqual(detail["match_type"], "explicit_package_size_fallback")

    def test_lookup_name_variants_strip_qualifiers(self):
        self.assertIn("scallions", mod._lookup_name_variants("fresh scallions"))
        self.assertIn("green onion", mod._lookup_name_variants("fresh green onions"))

    def test_implausible_whole_animal_llm_portion_is_rejected(self):
        with (
            patch.object(
                mod,
                "canonical_name_to_usda",
                return_value={
                    "usda_id": "01138",
                    "canonical": "whole duck",
                    "food_group": "Dairy and Egg Products",
                },
            ),
            patch.object(mod, "_estimate_grams_from_usda_id", side_effect=ValueError("No direct portion match")),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_estimate_grams_from_name_portion", side_effect=ValueError("No name match")),
            patch.object(
                mod,
                "_lookup_recipe1m_llm_portion_fallback",
                return_value={
                    "ingredient": "whole duck",
                    "unit": "whole",
                    "grams_per_unit": 150.0,
                    "sample_measurement": "1 whole",
                },
            ),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["whole duck"], ["1 whole"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [0.0])
        self.assertIsNone(detail["weight_grams"])
        self.assertIn("missing_portion_for_unit", detail["error"])
        self.assertEqual(detail["portion_match"]["rejected_reason"], "whole_animal_too_small")

    def test_llm_portion_verifier_rejects_implausible_and_conversion_defaults(self):
        # Tiny units that came back far too heavy.
        self.assertEqual(
            mod._llm_weight_plausibility_error(name="garlic", qty="3", unit="clove", grams=90.0),
            "clove_too_heavy",
        )
        self.assertEqual(
            mod._llm_weight_plausibility_error(name="basil", qty="1", unit="leaf", grams=8.0),
            "leaf_too_heavy",
        )
        # An ounce-in-grams constant leaking onto a non-mass unit.
        self.assertTrue(
            str(
                mod._llm_weight_plausibility_error(name="canned soup", qty="1", unit="can", grams=1000.0)
            ).startswith("suspicious_unit_conversion_default")
        )
        # > 5 kg per unit is never a real portion.
        self.assertEqual(
            mod._llm_weight_plausibility_error(name="mystery", qty="1", unit="piece", grams=7000.0),
            "implausible_per_unit_weight",
        )
        # Legitimate values must still pass.
        self.assertIsNone(mod._llm_weight_plausibility_error(name="garlic", qty="3", unit="clove", grams=12.0))
        self.assertIsNone(mod._llm_weight_plausibility_error(name="flour", qty="1", unit="lb", grams=453.59))
        self.assertIsNone(mod._llm_weight_plausibility_error(name="milk", qty="1", unit="liter", grams=1000.0))

    def test_food_class_compatibility_rejects_known_bad_pairs(self):
        self.assertEqual(
            mod._food_class_mismatch_reason("whole duck", "Egg, duck, whole", "Dairy and Egg Products"),
            "meat_poultry_vs_egg",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("Minute Rice", "Minute Steak", "Beef Products"),
            "grain_vs_meat_beef",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("gelatin powder", "Baobab powder", "Fruits and Fruit Juices"),
            "gelatin_thickener_vs_fruit",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("fresh scallions", "Basil, fresh", "Spices and Herbs"),
            "vegetable_vs_herb_spice",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("whole duck", "Pheasant, cooked, total edible", "Poultry Products"),
            "protein_identity_duck_vs_pheasant",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("whole duck", "Quail, cooked, total edible", "Poultry Products"),
            "protein_identity_duck_vs_quail",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("eggs", "Bread, egg", "Baked Products"),
            "egg_vs_baked_good",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason("ice cubes", "Frozen novelties, ice type, lime", "Sweets"),
            "ice_vs_frozen_novelty",
        )
        self.assertIsNone(
            mod._food_class_mismatch_reason(
                "sharp cheddar cheese",
                "Cheese, cheddar, sharp",
                "Dairy and Egg Products",
            )
        )
        self.assertEqual(
            mod._food_class_mismatch_reason(
                "garlic bagels",
                "Fruit leather pieces",
                "Fruits and Fruit Juices",
            ),
            "baked_good_vs_fruit",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason(
                "fat free vegetable soup",
                "Oil, vegetable",
                "Fats and Oils",
            ),
            "stock_sauce_vs_oil_fat",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason(
                "sheep tongue",
                "Cheese, sheep milk",
                "Dairy and Egg Products",
            ),
            "meat_lamb_goat_vs_dairy_cheese",
        )
        self.assertEqual(
            mod._food_class_mismatch_reason(
                "garlic bagels",
                "Bagels, wheat",
                "Baked Products",
            ),
            None,
        )
        self.assertEqual(
            mod._food_class_mismatch_reason(
                "Minute Rice",
                "Rice noodles, cooked",
                "Cereal Grains and Pasta",
            ),
            "head_token_rice_vs_noodle",
        )

    def test_hybrid_usda_link_uses_lexical_candidate_after_bad_vector_rejected(self):
        class FakeCollection:
            name = "fake_usda"

            def query(self, **kwargs):
                return {
                    "documents": [["Minute Steak"]],
                    "metadatas": [[
                        {
                            "usda_id": "23000",
                            "canonical_id": "steak",
                            "usda_food_label": "Minute Steak",
                            "food_group": "Beef Products",
                        }
                    ]],
                    "distances": [[0.05]],
                }

            def get(self, **kwargs):
                if kwargs.get("offset", 0) > 0:
                    return {"documents": [], "metadatas": []}
                return {
                    "documents": ["Rice, white, cooked"],
                    "metadatas": [
                        {
                            "usda_id": "20000",
                            "canonical_id": "rice",
                            "usda_food_label": "Rice, white, cooked",
                            "food_group": "Cereal Grains and Pasta",
                        }
                    ],
                }

        with (
            patch.object(mod, "_get_usda_links_collections", return_value=[FakeCollection()]),
            patch.object(mod, "get_embeddings", return_value=[0.0, 1.0]),
            patch.object(mod, "_foodon_compatibility", return_value=None),
        ):
            hit = mod._embedding_usda_link("Minute Rice")

        self.assertIsNotNone(hit)
        self.assertEqual(hit["usda_id"], "20000")
        self.assertEqual(hit["match_source"], "hybrid")
        self.assertGreater(hit["lexical_score"], 0)

    def test_foodon_incompatible_candidate_is_rejected_from_hybrid_match(self):
        class FakeCollection:
            name = "fake_usda"

            def query(self, **kwargs):
                return {
                    "documents": [["Bread, white"]],
                    "metadatas": [[
                        {
                            "usda_id": "18000",
                            "canonical_id": "bread",
                            "usda_food_label": "Bread, white",
                            "food_group": "Baked Products",
                        }
                    ]],
                    "distances": [[0.05]],
                }

            def get(self, **kwargs):
                return {"documents": [], "metadatas": []}

        with (
            patch.object(mod, "_get_usda_links_collections", return_value=[FakeCollection()]),
            patch.object(mod, "get_embeddings", return_value=[0.0, 1.0]),
            patch.object(mod, "_foodon_compatibility", return_value=False),
        ):
            self.assertIsNone(mod._embedding_usda_link("bread"))

    def test_common_reference_covers_gelatin_package(self):
        self.assertEqual(
            mod._common_unit_reference_grams("watermelon gelatin", "package"),
            (85.0, "gelatin dessert package"),
        )

    def test_common_reference_covers_yeast_packet(self):
        self.assertEqual(
            mod._common_unit_reference_grams("active dry yeast", "packet"),
            (7.0, "yeast packet"),
        )

    def test_common_reference_covers_tomato_can(self):
        self.assertEqual(
            mod._common_unit_reference_grams("diced tomatoes", "can"),
            (411.0, "standard tomato can"),
        )

    def test_bad_direct_usda_link_is_ignored(self):
        with (
            patch.object(
                mod,
                "canonical_name_to_usda",
                return_value={
                    "usda_id": "01138",
                    "canonical": "whole duck",
                    "usda_food_label": "Egg, duck, whole",
                    "food_group": "Dairy and Egg Products",
                },
            ),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_weight_name_usda_link", return_value=None),
            patch.object(mod, "_lookup_recipe1m_llm_weight_fallback", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["whole duck"], ["1 whole"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [0.0])
        self.assertIsNone(detail["usda_id"])
        self.assertEqual(detail["error"], "missing_usda_id")

    def test_implausible_recipe1m_llm_weight_fallback_is_rejected(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_weight_name_usda_link", return_value=None),
            patch.object(
                mod,
                "_lookup_recipe1m_llm_weight_fallback",
                return_value={
                    "ingredient": "whole duck",
                    "sample_measurement": "1 whole",
                    "grams": 150.0,
                    "signature": (1.0, "whole"),
                },
            ),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(["whole duck"], ["1 whole"])

        detail = result["details"][0]
        self.assertEqual(result["weights"], [0.0])
        self.assertIsNone(detail["weight_grams"])
        self.assertEqual(detail["error"], "missing_usda_id")

    def test_low_confidence_result_calls_live_llm(self):
        details = [
            {
                "name": "parsley",
                "parsed_quantity": "1",
                "parsed_unit": "pinch",
                "quantity_inferred": True,
                "unit_inferred": True,
                "match_type": "herb_pinch_fallback",
                "weight_grams": 0.3,
                "error": None,
            }
        ]
        weights = [0.3]

        with patch.object(mod, "_live_llm_weight_fallback", return_value=(1.2, None)):
            mod._apply_low_confidence_live_llm(details, weights)

        detail = details[0]
        self.assertEqual(weights, [1.2])
        self.assertTrue(detail["live_llm_fallback"])
        self.assertEqual(detail["live_llm_reason"], "low_confidence")
        self.assertEqual(detail["pre_llm_weight_grams"], 0.3)
        self.assertLess(detail["pre_llm_confidence"], mod.LIVE_LLM_CONFIDENCE_THRESHOLD)

    def test_low_confidence_live_llm_rejects_implausible_estimate_and_keeps_pre_llm(self):
        details = [
            {
                "name": "parsley",
                "parsed_quantity": "1",
                "parsed_unit": "pinch",
                "quantity_inferred": True,
                "unit_inferred": True,
                "match_type": "herb_pinch_fallback",
                "weight_grams": 0.3,
                "error": None,
            }
        ]
        weights = [0.3]

        with patch.object(mod, "_live_llm_weight_fallback", return_value=(90.0, None)):
            mod._apply_low_confidence_live_llm(details, weights)

        detail = details[0]
        # Implausible LLM value rejected; the deterministic result is kept.
        self.assertEqual(weights, [0.3])
        self.assertEqual(detail["weight_grams"], 0.3)
        self.assertEqual(detail["match_type"], "herb_pinch_fallback")
        self.assertFalse(detail["live_llm_fallback"])
        self.assertIn("rejected_llm_estimate", detail["live_llm_error"])

    def test_more_common_references_from_recipe1m_samples(self):
        with (
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
        ):
            result = self.invoke_debug(
                [
                    "green tea bags",
                    "sazon goya",
                    "compressed yeast cake",
                    "garlic bagels",
                    "Boboli Thin Pizza Shell",
                ],
                ["3", "2 envelopes", "1", "6", "1"],
            )

        self.assertEqual(result["weights"], [6.0, 10.0, 18.0, 570.0, 142.0])
        self.assertTrue(
            all(d["match_type"] == "common_unit_reference_fallback" for d in result["details"])
        )

    def test_leading_name_unit_not_used_when_measurement_has_its_own_unit(self):
        # "cup" leads the name but the measurement already carries a unit —
        # the measurement wins, the name prefix is not lifted.
        result = self.invoke_debug(["cup grated parmesan cheese"], ["2 tablespoons"])
        detail = result["details"][0]
        self.assertEqual(detail["parsed_unit"], "tablespoon")
        self.assertIsNone(detail["error"])

    def test_package_size_prefix_fix_keeps_multiplier_and_explicit_count(self):
        with (
            patch.object(mod, "_live_llm_weight_fallback", return_value=(None, "disabled")),
            patch.object(mod, "canonical_name_to_usda", return_value=None),
            patch.object(mod, "_embedding_usda_link", return_value=None),
            patch.object(mod, "_weight_name_usda_link", return_value=None),
        ):
            result = self.invoke_debug(
                ["salmon fillets", "crushed tomatoes", "crushed tomatoes"],
                ["2 x 120g", "1 (28 oz) can", "28 oz can"],
            )
        d_mul, d_explicit, d_bare = result["details"]
        self.assertAlmostEqual(d_mul["weight_grams"], 240.0, delta=0.5)
        # "1 (28 oz) can" and the bare "28 oz can" must both be one ~794 g can.
        self.assertAlmostEqual(d_explicit["weight_grams"], 28 * 28.349523125, delta=1.0)
        self.assertEqual(d_explicit["match_type"], "explicit_package_size_fallback")
        self.assertAlmostEqual(d_bare["weight_grams"], d_explicit["weight_grams"], delta=0.5)
        self.assertEqual(d_bare["match_type"], "explicit_package_size_fallback")

    def test_recipe1m_llm_fallback_csvs_are_gated_off_by_default(self):
        # Default env: both Recipe1M LLM CSV lookups are short-circuited.
        self.assertFalse(mod.RECIPE1M_LLM_FALLBACK_ENABLED)
        self.assertIsNone(
            mod._lookup_recipe1m_llm_weight_fallback("nonexistent thing", "1 cup", "1", "cup")
        )
        self.assertIsNone(mod._lookup_recipe1m_llm_portion_fallback("nonexistent thing", "slice"))

    def test_offline_reference_index_silently_empty_without_postgres(self):
        # The runtime reads the reference dataset from Postgres; if the entry is
        # missing the loader must no-op, never raise.
        mod._load_offline_reference_dataset_index.cache_clear()
        try:
            index = mod._load_offline_reference_dataset_index()
        except Exception as exc:  # pragma: no cover - this is the thing we forbid
            self.fail(f"offline reference loader raised: {exc!r}")
        self.assertIsInstance(index, dict)


if __name__ == "__main__":
    unittest.main()
