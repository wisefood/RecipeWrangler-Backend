import unittest
from unittest.mock import patch

from recipe_wrangler.tools import nutrition_match as nm


def _cand(name: str, distance: float) -> dict:
    return {"document": name, "metadata": {"food_name": name}, "distance": distance}


class CleanQueryTests(unittest.TestCase):
    def test_strips_prep_qualifiers_and_leading_quantity(self):
        cases = {
            "Boneless, skinless chicken breast (about 1 lb), finely chopped": "boneless skinless chicken breast",
            "garlic, finely chopped": "garlic",
            "2 1/2 cups all-purpose flour": "all-purpose flour",
            "low-fat yoghurt": "low-fat yoghurt",
            "1 (28 oz) can crushed tomatoes": "canned crushed tomatoes",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(nm.clean_query(raw), want)

    def test_plain_name_passes_through(self):
        for name in ("arugula", "chuck", "snow crab legs", "olive oil"):
            self.assertIn(name.split()[0], nm.clean_query(name))

    def test_preserves_nutrition_defining_qualifiers(self):
        for name in (
            "cooked green lentils",
            "red bell pepper",
            "cherry tomatoes",
            "unsweetened applesauce",
            "skinless chicken breast",
            "dried chickpeas",
        ):
            self.assertEqual(nm.clean_query(name), name)

    def test_can_container_is_normalized_to_canned_state(self):
        self.assertEqual(
            nm.clean_query("cans no-added-salt chickpeas, rinsed, drained"),
            "canned chickpeas",
        )


class FoodClassTests(unittest.TestCase):
    def test_class_assignment(self):
        self.assertEqual(nm.food_class("chicken breast"), "animal_protein")
        self.assertEqual(nm.food_class("boneless rib-eye steak"), "animal_protein")
        self.assertEqual(nm.food_class("low-fat yoghurt"), "dairy")
        self.assertEqual(nm.food_class("tofu yogurt"), "plant_milk")
        self.assertEqual(nm.food_class("arugula"), "leafy_green")
        self.assertEqual(nm.food_class("spices, fenugreek seed"), "spice_herb")
        self.assertEqual(nm.food_class("chianti wine"), "alcohol")
        self.assertEqual(nm.food_class("rice, white, italian arborio risotto, raw"), "grain_cereal")
        self.assertEqual(nm.food_class("eggplant"), "vegetable")
        self.assertEqual(nm.food_class("egg"), "egg")
        self.assertEqual(nm.food_class("salad with a little dressing"), "salad")
        self.assertEqual(nm.food_class("green olives"), "fruit")
        self.assertEqual(nm.food_class("no-added-salt chopped tomatoes"), "vegetable")
        self.assertEqual(nm.food_class("Rusk, no added salt"), "grain_cereal")
        self.assertEqual(nm.food_class("Salad dressing vinaigrette"), "condiment_sauce")
        self.assertEqual(nm.food_class("steamed Asian greens"), "leafy_green")
        self.assertEqual(nm.food_class("Pastries, Asian"), "grain_cereal")
        self.assertEqual(nm.food_class("spring cold cuts"), "animal_protein")
        self.assertEqual(nm.food_class("something unmappable xyz"), "other")

    def test_hard_incompatibilities(self):
        self.assertFalse(nm.classes_compatible("dairy", "plant_milk"))
        self.assertFalse(nm.classes_compatible("animal_protein", "dairy"))
        self.assertFalse(nm.classes_compatible("leafy_green", "spice_herb"))
        self.assertFalse(nm.classes_compatible("alcohol", "grain_cereal"))
        self.assertFalse(nm.classes_compatible("egg", "vegetable"))
        self.assertFalse(nm.classes_compatible("salad", "condiment_sauce"))
        # allowed / too-ambiguous-to-reject
        self.assertTrue(nm.classes_compatible("animal_protein", "animal_protein"))
        self.assertTrue(nm.classes_compatible("dairy", "other"))
        self.assertTrue(nm.classes_compatible("vegetable", "condiment_sauce"))
        self.assertFalse(nm.classes_compatible("animal_protein", "condiment_sauce"))
        self.assertFalse(nm.classes_compatible("legume", "nut_seed"))
        self.assertFalse(nm.classes_compatible("leafy_green", "condiment_sauce"))


class Bm25Tests(unittest.TestCase):
    def test_bm25_ranks_overlap_higher(self):
        scores = nm._bm25_scores(
            ["chicken", "breast"],
            [["chicken", "breast", "raw"], ["beef", "rump", "steak"], ["chicken", "broth"]],
        )
        self.assertEqual(max(range(len(scores)), key=lambda i: scores[i]), 0)
        self.assertGreater(scores[2], scores[1])  # "chicken broth" beats "beef rump steak"


class BestNutritionMatchTests(unittest.TestCase):
    def _patch_pools(self, irish=None, eu=None):
        return (
            patch.object(nm, "query_irish_nutrition_candidates", return_value=irish or []),
            patch.object(nm, "query_eu_nutrition_candidates", return_value=eu or []),
        )

    def test_irish_uses_eu_as_its_only_fallback(self):
        p1, p2 = self._patch_pools(
            eu=[_cand("Chicken breast, raw", 0.18)],
        )
        with p1, p2:
            r = nm.best_nutrition_match("chicken breast", "irish")
        self.assertEqual(r["source_key"], "eu")
        self.assertEqual(r["matched_name"], "Chicken breast, raw")

    def test_slovenian_and_eu_candidates_compete_in_one_pool(self):
        with (
            patch.object(
                nm,
                "query_slovenian_nutrition_candidates",
                return_value=[_cand("Apple sauce", 0.30)],
            ),
            patch.object(
                nm,
                "query_eu_nutrition_candidates",
                return_value=[_cand("Apple, raw", 0.12)],
            ),
        ):
            r = nm.best_nutrition_match("apple", "slovenian")
        self.assertEqual(r["source_key"], "eu")
        self.assertEqual(r["matched_name"], "Apple, raw")

    def test_slovenian_wins_when_it_is_the_best_match(self):
        with (
            patch.object(
                nm,
                "query_slovenian_nutrition_candidates",
                return_value=[_cand("Potato, raw", 0.10)],
            ),
            patch.object(
                nm,
                "query_eu_nutrition_candidates",
                return_value=[_cand("Potato starch", 0.20)],
            ),
        ):
            r = nm.best_nutrition_match("potato", "slovenian")
        self.assertEqual(r["source_key"], "slovenian")
        self.assertEqual(r["matched_name"], "Potato, raw")

    def test_usda_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported nutrition source"):
            nm.best_nutrition_match("chicken breast", "usda")

    def test_strong_match_on_token_overlap(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Chicken breast, raw", 0.18), _cand("Chicken broth", 0.30)],
        )
        with p1, p2:
            r = nm.best_nutrition_match("chicken breast", "irish")
        self.assertEqual(r["confidence"], "strong")
        self.assertEqual(r["matched_name"], "Chicken breast, raw")

    def test_matching_does_not_read_neo4j(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Chicken breast, raw", 0.18)],
        )
        with (
            p1,
            p2,
            patch(
                "recipe_wrangler.utils.neo4j_utils.run_query",
                side_effect=AssertionError("nutrition matching must be ES-only"),
            ),
        ):
            result = nm.best_nutrition_match("chicken breast", "irish")
        self.assertEqual(result["matched_name"], "Chicken breast, raw")

    def test_zero_overlap_attractor_is_demoted(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Rice, white, Italian Arborio risotto, raw", 0.34),
                   _cand("Wine, table, red", 0.48)],
        )
        with p1, p2:
            r = nm.best_nutrition_match("chianti wine", "irish")
        self.assertEqual(r["matched_name"], "Wine, table, red")

    def test_food_class_guard_rejects_incompatible(self):
        p1, p2 = self._patch_pools(irish=[_cand("Tofu yogurt", 0.16)])
        with p1, p2:
            r = nm.best_nutrition_match("low-fat yoghurt", "irish")
        self.assertEqual(r["confidence"], "none")
        self.assertIsNone(r["match"])

    def test_food_class_guard_prefers_compatible(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Tofu yogurt", 0.16), _cand("Yogurt, plain, low fat", 0.22)],
        )
        with p1, p2:
            r = nm.best_nutrition_match("low-fat yoghurt", "irish")
        self.assertEqual(r["matched_name"], "Yogurt, plain, low fat")

    def test_species_guard_prevents_cod_from_matching_pork_or_beef_fillet(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Pork fillet raw", 0.05),
                _cand("Beef fillet tenderloin", 0.08),
                _cand("Cod fillet raw", 0.22),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("fresh cod fillet", "irish")
        self.assertEqual(result["matched_name"], "Cod fillet raw")

    def test_trailing_spray_oil_does_not_turn_pumpkin_into_seed_oil(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Pumpkin seed oil", 0.03),
                _cand("Pumpkin, raw", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "pumpkin, peeled, cut in 2cm pieces spray oil", "irish"
            )
        self.assertEqual(result["cleaned_query"], "pumpkin cut in 2cm pieces")
        self.assertEqual(result["matched_name"], "Pumpkin, raw")

    def test_standalone_spray_oil_still_profiles_as_oil(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Oil spray", 0.15)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("spray oil", "irish")
        self.assertEqual(result["cleaned_query"], "spray oil")
        self.assertEqual(result["matched_name"], "Oil spray")

    def test_spray_oil_rejects_brand_name_spray_attractor(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Juice drink Ocean Spray Cranberry classic", 0.02),
                _cand("Oil spray", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("spray oil", "irish")
        self.assertEqual(result["matched_name"], "Oil spray")

    def test_lamb_cutlet_rejects_french_dressing_attractor(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Dressing, French", 0.02),
                _cand("Lamb cutlet, raw", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "French-trimmed lamb cutlets", "irish"
            )
        self.assertEqual(result["matched_name"], "Lamb cutlet, raw")

    def test_liquid_stock_rejects_concentrated_cube_nutrition(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Stock cubes, chicken", 0.02),
                _cand("Chicken stock, liquid", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "boiling salt-reduced liquid chicken stock", "irish"
            )
        self.assertEqual(result["matched_name"], "Chicken stock, liquid")

    def test_pumpkin_flesh_rejects_seed_product(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Pumpkin and squash, seed, dried", 0.02),
                _cand("Pumpkin, flesh, raw", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("squash or pumpkin, deseeded", "irish")
        self.assertEqual(result["matched_name"], "Pumpkin, flesh, raw")

    def test_chickpea_rejects_peanut_attractor(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Peanut, no added salt", 0.02),
                _cand("Chickpeas, canned, drained", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "no-added-salt chickpeas, rinsed, drained", "irish"
            )
        self.assertEqual(result["matched_name"], "Chickpeas, canned, drained")

    def test_chickpea_and_garbanzo_tokens_share_identity(self):
        self.assertIn("chickpea", nm._tokens("chickpeas"))
        self.assertIn("chickpea", nm._tokens("garbanzos"))

    def test_canned_chickpeas_reject_unspecified_dry_row(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Chickpeas", 0.02),
                _cand("Chickpeas, canned, drained", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("can chickpeas, drained", "irish")
        self.assertEqual(result["matched_name"], "Chickpeas, canned, drained")

    def test_tuna_in_water_rejects_plain_spring_water(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Spring water, bottled", 0.02),
                _cand("Tuna, canned in spring water, drained", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "tuna in spring water, drained", "irish"
            )
        self.assertEqual(result["matched_name"], "Tuna, canned in spring water, drained")

    def test_spinach_with_dressing_rejects_pure_vinaigrette(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Salad dressing vinaigrette", 0.02),
                _cand("Spinach, raw", 0.24),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "baby spinach dressed with balsamic vinaigrette", "irish"
            )
        self.assertEqual(result["matched_name"], "Spinach, raw")

    def test_olive_does_not_match_beef_olives(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Beef olives raw", 0.04), _cand("Olives green", 0.25)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("olives sliced", "irish")
        self.assertEqual(result["matched_name"], "Olives green")

    def test_no_candidates_returns_none(self):
        p1, p2 = self._patch_pools()
        with p1, p2:
            r = nm.best_nutrition_match("zzz nonexistent", "irish")
        self.assertEqual(r["confidence"], "none")
        self.assertIsNone(r["match"])


if __name__ == "__main__":
    unittest.main()
