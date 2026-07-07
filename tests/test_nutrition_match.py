import unittest
from unittest.mock import patch

from recipe_wrangler.tools import nutrition_match as nm


def _cand(name: str, distance: float) -> dict:
    return {"document": name, "metadata": {"food_name": name}, "distance": distance}


class CleanQueryTests(unittest.TestCase):
    def test_strips_prep_qualifiers_and_leading_quantity(self):
        cases = {
            "Boneless, skinless chicken breast (about 1 lb), finely chopped": "chicken breast",
            "garlic, finely chopped": "garlic",
            "2 1/2 cups all-purpose flour": "all-purpose flour",
            "low-fat yoghurt": "yoghurt",
            "1 (28 oz) can crushed tomatoes": "tomatoes",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(nm.clean_query(raw), want)

    def test_plain_name_passes_through(self):
        for name in ("arugula", "chuck", "snow crab legs", "olive oil"):
            self.assertIn(name.split()[0], nm.clean_query(name))


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
        self.assertEqual(nm.food_class("something unmappable xyz"), "other")

    def test_hard_incompatibilities(self):
        self.assertFalse(nm.classes_compatible("dairy", "plant_milk"))
        self.assertFalse(nm.classes_compatible("animal_protein", "dairy"))
        self.assertFalse(nm.classes_compatible("leafy_green", "spice_herb"))
        self.assertFalse(nm.classes_compatible("alcohol", "grain_cereal"))
        self.assertFalse(nm.classes_compatible("egg", "vegetable"))
        # allowed / too-ambiguous-to-reject
        self.assertTrue(nm.classes_compatible("animal_protein", "animal_protein"))
        self.assertTrue(nm.classes_compatible("dairy", "other"))
        self.assertTrue(nm.classes_compatible("vegetable", "condiment_sauce"))
        self.assertTrue(nm.classes_compatible("animal_protein", "condiment_sauce"))


class FoodOnVariantTests(unittest.TestCase):
    def test_variants_strip_form_words_and_apply_synonyms(self):
        self.assertIn("garlic", nm._foodon_name_variants("Garlic, raw"))
        self.assertIn("yogurt", nm._foodon_name_variants("low-fat yoghurt"))
        self.assertIn("yogurt", nm._foodon_name_variants("Yogurt, plain, low fat"))
        self.assertIn("arugula", nm._foodon_name_variants("rocket"))
        # the original (lowercased) is always included
        self.assertEqual(nm._foodon_name_variants("Garlic, raw")[0], "garlic, raw")

    def test_foodon_compatible_neutral_when_unresolvable(self):
        # An obviously-not-an-ingredient name on at least one side -> None (neutral),
        # never raises, even if the weight tool / Neo4j is unavailable.
        with patch.object(nm, "_foodon_class_ids", side_effect=lambda n: () if "zzz" in n else ("FOODON_X",)):
            self.assertIsNone(nm._foodon_compatible("zzz nothing", "tomato"))
            self.assertIsNone(nm._foodon_compatible("tomato", "zzz nothing"))


class Bm25Tests(unittest.TestCase):
    def test_bm25_ranks_overlap_higher(self):
        scores = nm._bm25_scores(
            ["chicken", "breast"],
            [["chicken", "breast", "raw"], ["beef", "rump", "steak"], ["chicken", "broth"]],
        )
        self.assertEqual(max(range(len(scores)), key=lambda i: scores[i]), 0)
        self.assertGreater(scores[2], scores[1])  # "chicken broth" beats "beef rump steak"


class BestNutritionMatchTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise USDA fallback behavior independently of runtime
        # regional configuration (production may select the EU fallback pool).
        self._fallback_patch = patch.object(
            nm, "NUTRITION_FALLBACK_SOURCE", "usda"
        )
        self._fallback_patch.start()

    def tearDown(self):
        self._fallback_patch.stop()

    def _patch_pools(self, irish=None, usda=None):
        # bypass the alias table + curated table to test the vector/rerank path
        return (
            patch.object(nm, "query_irish_nutrition_candidates", return_value=irish or []),
            patch.object(nm, "query_usda_nutrition_candidates", return_value=usda or []),
            patch.object(nm, "_curated_link_index", return_value={}),
            patch.object(nm, "_alias_index", return_value={}),
        )

    def test_alias_table_wins_outright(self):
        idx = {"chicken breast": {"usda_id": "05062",
                                  "label": "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw"}}
        with patch.object(nm, "_alias_index", return_value=idx):
            r = nm.best_nutrition_match("boneless skinless chicken breast", "us")
        self.assertEqual(r["confidence"], "alias")
        self.assertEqual(r["source_key"], "usda")
        self.assertEqual(r["match"]["metadata"]["usda_id"], "05062")

    def test_curated_link_competes_and_wins_when_strong(self):
        idx = {"chicken breast": {"usda_id": "05064", "sim": 0.95,
                                  "label": "Chicken, broilers or fryers, breast, meat only, raw"}}
        with patch.object(nm, "_curated_link_index", return_value=idx), \
             patch.object(nm, "_alias_index", return_value={}):
            r = nm.best_nutrition_match("Boneless, skinless chicken breast", "irish")
        self.assertEqual(r["confidence"], "curated")
        self.assertEqual(r["source_key"], "usda")
        self.assertEqual(r["match"]["metadata"]["usda_id"], "05064")

    def test_curated_link_loses_to_a_better_vector_candidate(self):
        idx = {"all-purpose flour": {"usda_id": "11413", "sim": 0.86, "label": "Potato flour"}}
        with patch.object(nm, "query_irish_nutrition_candidates", return_value=[]), \
             patch.object(nm, "query_usda_nutrition_candidates",
                          return_value=[_cand("Wheat flour, white, all-purpose, unenriched", 0.11)]), \
             patch.object(nm, "_curated_link_index", return_value=idx), \
             patch.object(nm, "_alias_index", return_value={}):
            r = nm.best_nutrition_match("all-purpose flour", "irish")
        self.assertEqual(r["matched_name"], "Wheat flour, white, all-purpose, unenriched")
        self.assertNotEqual(r["match"]["metadata"].get("usda_id"), "11413")

    def test_strong_match_on_token_overlap(self):
        p1, p2, p3, p4 = self._patch_pools(
            irish=[_cand("Chicken breast, raw", 0.18), _cand("Chicken broth", 0.30)],
        )
        with p1, p2, p3, p4:
            r = nm.best_nutrition_match("chicken breast", "irish")
        self.assertEqual(r["confidence"], "strong")
        self.assertEqual(r["matched_name"], "Chicken breast, raw")

    def test_zero_overlap_attractor_is_demoted(self):
        p1, p2, p3, p4 = self._patch_pools(
            irish=[_cand("Rice, white, Italian Arborio risotto, raw", 0.34),
                   _cand("Wine, table, red", 0.48)],
        )
        with p1, p2, p3, p4:
            r = nm.best_nutrition_match("chianti wine", "irish")
        self.assertEqual(r["matched_name"], "Wine, table, red")

    def test_food_class_guard_rejects_incompatible(self):
        p1, p2, p3, p4 = self._patch_pools(irish=[_cand("Tofu yogurt", 0.16)])
        with p1, p2, p3, p4:
            r = nm.best_nutrition_match("low-fat yoghurt", "irish")
        self.assertEqual(r["confidence"], "none")
        self.assertIsNone(r["match"])

    def test_food_class_guard_prefers_compatible(self):
        p1, p2, p3, p4 = self._patch_pools(
            irish=[_cand("Tofu yogurt", 0.16), _cand("Yogurt, plain, low fat", 0.22)],
        )
        with p1, p2, p3, p4:
            r = nm.best_nutrition_match("low-fat yoghurt", "irish")
        self.assertEqual(r["matched_name"], "Yogurt, plain, low fat")

    def test_no_candidates_returns_none(self):
        p1, p2, p3, p4 = self._patch_pools()
        with p1, p2, p3, p4:
            r = nm.best_nutrition_match("zzz nonexistent", "irish")
        self.assertEqual(r["confidence"], "none")
        self.assertIsNone(r["match"])


if __name__ == "__main__":
    unittest.main()
