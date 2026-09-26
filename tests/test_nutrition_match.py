import re
import unittest
from unittest.mock import patch

from recipe_wrangler.tools import nutrition_match as nm
from recipe_wrangler.tools import nutritional_calculator as calc


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

    def test_reduced_sodium_qualifier_is_stripped(self):
        # Found in the same outlier sweep as the soda-water bug: "reduced-
        # sodium peas" was matching "Sodium bicarbonate" on the shared
        # literal word "sodium" -- 34,656mg sodium for a can of peas.
        self.assertEqual(nm.clean_query("of reduced-sodium peas"), "of peas")
        self.assertEqual(nm.clean_query("low-sodium soy sauce"), "soy sauce")
        self.assertEqual(nm.clean_query("no added sodium beans"), "beans")

    def test_preserves_nutrition_defining_qualifiers(self):
        for name in (
            "cooked green lentils",
            "red bell pepper",
            "cherry tomatoes",
            "skinless chicken breast",
            "dried chickpeas",
        ):
            self.assertEqual(nm.clean_query(name), name)
        self.assertEqual(
            nm.clean_query("unsweetened applesauce"),
            "unsweetened apple sauce",
        )

    def test_can_container_is_normalized_to_canned_state(self):
        self.assertEqual(
            nm.clean_query("cans no-added-salt chickpeas, rinsed, drained"),
            "canned chickpeas",
        )

    def test_strips_synonym_collapse_residue(self):
        cases = {
            "courgette zucchini x s zucchini": "courgette zucchini",
            "chickpea garbanzo x s garbanzos x": "chickpea garbanzo garbanzos",
            "plain flour all purpose flour X": "plain flour all purpose flour",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(nm.clean_query(raw), want)

    def test_strips_leading_plus_and_unicode_fractions(self):
        self.assertEqual(nm.clean_query("plus 1 tablespoon white sugar"), "white sugar")
        self.assertEqual(nm.clean_query("½ tablespoons cocoa powder"), "cocoa powder")

    def test_parenthesised_quantity_prefix_is_dropped(self):
        self.assertEqual(nm.clean_query("(6 cups) water"), "water")

    def test_chilled_and_to_top_up_are_stripped(self):
        # Found while chasing the soda-water/bicarbonate mismatch: two
        # spellings of the same drink line normalized to different alias
        # keys ("chilled soda water..." vs "...soda water x to top up").
        self.assertEqual(
            nm.clean_query("chilled soda water club soda seltzer water"),
            "soda water club soda seltzer water",
        )
        self.assertEqual(
            nm.clean_query("soda water club soda seltzer water x to top up"),
            "soda water club soda seltzer water",
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

    def test_plural_forms_share_singular_class(self):
        # "baking apples" / "baking potatoes" fell through to "other" (only
        # the singular forms were in the class patterns) and then matched
        # "Baking powder" on the bare word "baking" -- found in the 2026-09-16
        # curation pass. food_class() now retries singularized tokens.
        self.assertEqual(nm.food_class("apples"), "fruit")
        self.assertEqual(nm.food_class("baking apples"), "fruit")
        self.assertEqual(nm.food_class("oranges"), "fruit")
        self.assertEqual(nm.food_class("baking potatoes"), "vegetable")

    def test_animal_kind_covers_animal_protein_vocabulary(self):
        # Found 2026-09-21: animal_kind() only recognized ~19 species/cuts
        # while the broader animal_protein class regex recognizes ~60 terms.
        # Anything in the gap got food_class="animal_protein" but
        # animal_kind=None, so animal_kinds_compatible() failed open --
        # confirmed causing a STRONG-confidence match of "ground sausage" to
        # "Coffee, ground" and "round steak" (beef) to "Pork, round steak,
        # raw". This test locks the two vocabularies together going forward:
        # every single-species animal_protein word must resolve to a real
        # animal_kind, or be explicitly listed below as one of the words
        # that's deliberately unmapped because it's genuinely
        # species-ambiguous (chicken liver vs beef liver are nutritionally
        # very different, so guessing a species would be its own bug).
        deliberately_ambiguous = {
            "sausage", "sausages", "chorizo", "salami", "pepperoni",
            "kielbasa", "bratwurst", "liver", "kidney", "tripe", "gizzard",
            "cold", "cuts", "cut", "meat",
        }
        # Fragments left over from splitting the raw regex source on
        # [a-z]+ boundaries (e.g. "anchov(?:y|ies)" -> "anchov"/"ies",
        # "rib[- ]?eye" -> "rib"/"eye") -- not real standalone words, the
        # actual words ("anchovy", "ribeye") are already covered.
        regex_artifacts = {"anchov", "ies", "rib", "eye"}
        words = set()
        for cls, pattern in nm._CLASS_PATTERNS:
            if cls != "animal_protein":
                continue
            for raw in re.findall(r"[a-z]+", pattern):
                if len(raw) < 3 or raw in regex_artifacts:
                    continue
                words.add(raw)
        uncovered = sorted(
            w for w in words
            if w not in deliberately_ambiguous
            and nm.animal_kind(f"{w} raw") is None
        )
        self.assertEqual(
            uncovered, [],
            f"animal_protein words with no animal_kind mapping: {uncovered} "
            "-- either add them to _ANIMAL_KIND_PATTERNS or to this test's "
            "deliberately_ambiguous set with a reason",
        )

    def test_animal_kind_terms_classify_as_animal_protein(self):
        # Reverse of the check above: a term recognized as a specific
        # species (animal_kind) but not recognized by the broader
        # animal_protein class regex is a silent gap in the other
        # direction -- food_class() would call it "other", which the
        # "other is always compatible" rule then lets match anything.
        # Found 2026-09-21: "eel" was in _ANIMAL_KIND_PATTERNS but not in
        # the animal_protein class pattern.
        uncovered = sorted(
            kind for kind, pattern in nm._ANIMAL_KIND_PATTERNS
            if nm.food_class(f"{kind} raw") != "animal_protein"
        )
        self.assertEqual(
            uncovered, [],
            f"animal_kind terms not recognized by food_class as "
            f"animal_protein: {uncovered} -- add them to the "
            "animal_protein class pattern",
        )

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
    def setUp(self):
        # Isolate from the real pipeline_static_data alias table — these tests
        # exercise the ES-pool ranking, not the curated short-circuit.
        nm._alias_index.cache_clear()
        patcher = patch.object(nm, "load_pipeline_data", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(nm._alias_index.cache_clear)

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
                "boiling liquid chicken stock", "irish"
            )
        self.assertEqual(result["matched_name"], "Chicken stock, liquid")

    def test_stock_hard_gate_extends_beyond_cubes_to_other_concentrate_forms(self):
        # Found in the 2026-09-16 plausibility audit: 42% of implausible
        # lines were a plain stock/broth query matching a concentrate
        # candidate by a different word than "cube" (gel, dehydrated, ...).
        for query, rejected_candidate in (
            ("vegetable stock", "Stock gel"),
            ("reduced-salt beef stock", "Broth or stock, beef, dehydrated"),
        ):
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(
                    irish=[_cand(rejected_candidate, 0.02)],
                )
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])

        # A dried noodle query still must not become an entire soup merely
        # because the soup label contains the word noodle.
        p1, p2 = self._patch_pools(
            irish=[_cand("Soup, chicken noodle, dried", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("dried noodles", "irish")
        self.assertIsNone(result["match"])

    def test_soda_water_rejects_bicarbonate_of_soda(self):
        # Found as the worst plausibility outlier after the 2026-09-17
        # recompute: "soda water" matched to the raising agent, not a drink
        # -- 68,484mg sodium in one serving.
        p1, p2 = self._patch_pools(
            irish=[_cand("Bicarbonate of soda", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("soda water club soda seltzer water", "irish")
        self.assertIsNone(result["match"])

    def test_bicarbonate_query_still_matches_bicarbonate(self):
        # The gate is scoped to "water" queries without "bicarbonate" in them
        # -- a genuine bicarbonate-of-soda query must still work.
        p1, p2 = self._patch_pools(
            irish=[_cand("Bicarbonate of soda", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("bicarbonate of soda", "irish")
        self.assertEqual(result["matched_name"], "Bicarbonate of soda")

    def test_cut_into_cubes_rejects_stock_cube_product(self):
        # "roasted pumpkin cubes" matched "Stock cubes, vegetable" on the
        # shared word "cubes" -- shape vs. concentrate-product collision.
        # Found in the 2026-09-17 outlier sweep alongside the soda-water bug.
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Stock cubes, vegetable", 0.02),
                _cand("Pumpkin, roasted/baked", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("roasted pumpkin cubes", "irish")
        self.assertEqual(result["matched_name"], "Pumpkin, roasted/baked")

    def test_hot_water_rejects_hot_pepper_candidate(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Pepper red hot paste", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("hot water", "irish")
        self.assertIsNone(result["match"])

    def test_hot_pepper_sauce_still_matches_hot_pepper(self):
        # The gate is scoped to water/milk queries -- a genuine hot-pepper
        # ingredient must not be blocked from its own record.
        p1, p2 = self._patch_pools(
            irish=[_cand("Pepper red hot paste", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("hot pepper sauce", "irish")
        self.assertEqual(result["matched_name"], "Pepper red hot paste")

    def test_flavoured_descriptor_rejects_seasoning_liquid_product(self):
        # "flavoured tomatoes" matched the commercial product "Seasoning
        # flavoured liquid" on the shared word "flavoured".
        p1, p2 = self._patch_pools(
            irish=[_cand("Seasoning flavoured liquid", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("flavoured tomatoes", "irish")
        self.assertIsNone(result["match"])

    def test_zero_alcohol_rejects_pure_alcohol(self):
        # "zero alcohol italian spritz" matched "Pure alcohol" -- a negation
        # failure (the opposite of what the recipe means).
        p1, p2 = self._patch_pools(
            irish=[_cand("Pure alcohol", 0.02)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("zero alcohol italian spritz", "irish")
        self.assertIsNone(result["match"])

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

    def test_shared_modifier_cannot_overturn_better_semantic_match(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("European sprat, raw", 0.327),
                _cand("Salad, green", 0.173),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "european blend salad greens",
                "irish",
                identity_name="salad",
            )
        self.assertEqual(result["matched_name"], "Salad, green")

    def test_wrong_top_identity_is_rejected_not_replaced(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Black pudding", 0.01),
                _cand("Rice, raw", 0.40),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "black rice", "irish", identity_name="rice"
            )
        self.assertIsNone(result["match"])
        self.assertEqual(result["reason"], "top_candidate_incompatible")

    def test_unrequested_high_impact_form_is_demoted(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Chicken skin, raw", 0.05),
                _cand("Chicken meat, raw", 0.30),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "chicken", "irish", identity_name="chicken"
            )
        self.assertEqual(result["matched_name"], "Chicken meat, raw")

    def test_non_food_is_rejected_before_retrieval(self):
        for name in (
            "wooden skewers", "baking paper", "glass jars",
            "drinking glasses", "ice-block sticks", "jar", "oven bag",
        ):
            with self.subTest(name=name), patch.object(
                nm,
                "query_irish_nutrition_candidates",
                side_effect=AssertionError("non-food must not reach retrieval"),
            ):
                result = nm.best_nutrition_match(name, "irish")
            self.assertIsNone(result["match"])
            self.assertEqual(result["reason"], "non_food")

    def test_seed_identity_does_not_collapse_to_the_parent_plant(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Pumpkin, cooked", 0.05),
                _cand("Pumpkin seeds, toasted", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "pumpkin seeds toasted",
                "irish",
                identity_name="pumpkin seeds",
            )
        self.assertEqual(result["matched_name"], "Pumpkin seeds, toasted")

    def test_plain_fish_rejects_composite_fish_product(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Fishcakes, white fish, baked", 0.05),
                _cand("European whitefish, raw", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "white fish", "irish", identity_name="fish"
            )
        self.assertEqual(result["matched_name"], "European whitefish, raw")

    def test_plain_potato_does_not_become_sweet_potato(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Sweet potato, cooked", 0.05),
                _cand("Potato, cooked", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "potato cooked", "irish", identity_name="potato"
            )
        self.assertEqual(result["matched_name"], "Potato, cooked")

    def test_baked_potato_does_not_become_potato_crisps(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Potato crisps, oven baked", 0.05),
                _cand("Potato, baked", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "baked potato", "irish", identity_name="potato"
            )
        self.assertEqual(result["matched_name"], "Potato, baked")

    def test_named_cooking_method_is_preserved(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Beans, pinto, refried", 0.05),
                _cand("Baked beans, canned", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "baked beans", "irish", identity_name="beans"
            )
        self.assertEqual(result["matched_name"], "Baked beans, canned")

    def test_missing_product_source_is_distinct_from_conflicting_source(self):
        self.assertEqual(
            nm._identity_match_level("rice vinegar", "Vinegar"),
            "safe_parent",
        )
        self.assertEqual(
            nm._identity_match_level("rice vinegar", "Wine vinegar"),
            "incompatible",
        )
        self.assertEqual(
            nm._identity_match_level("vanilla paste", "Curry paste"),
            "incompatible",
        )

    def test_safe_generic_parent_is_accepted_and_labelled(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Vinegar", 0.10)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("rice vinegar", "irish")
        self.assertEqual(result["matched_name"], "Vinegar")
        self.assertEqual(result["confidence"], "strong")
        self.assertEqual(result["reason"], "safe_parent_fallback")

    def test_or_alternatives_choose_one_supported_food_and_explain_it(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Margarine, average", 0.20),
                _cand("Butter, unsalted", 0.10),
                _cand("Oil, vegetable, average", 0.15),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "margarine or spread or butter", "irish"
            )
        self.assertEqual(result["matched_name"], "Butter, unsalted")
        self.assertEqual(result["selected_alternative"], "butter")
        self.assertEqual(result["reason"], "alternative_selected:butter")
        self.assertIn("Nutrition calculated using 'butter'", result["nutrition_match_note"])

    def test_or_prefers_complete_final_branch_and_carries_food_to_a_cut(self):
        cases = (
            ("orange or red bell pepper", "Peppers, capsicum, red, raw", "red bell pepper"),
            ("vegetable or beef stock", "Beef stock", "beef stock"),
            ("white fish fillet or steak", "European whitefish, raw", "white fish steak"),
        )
        for query, candidate, selected in cases:
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertEqual(result["matched_name"], candidate)
                self.assertEqual(result["selected_alternative"], selected)

    def test_or_carries_wholegrain_qualifier_to_final_pasta_branch(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Pasta, dry, wholewheat, raw", 0.05)]
        )
        with p1, p2:
            result = nm.best_nutrition_match(
                "wholewheat fusilli or penne pasta", "irish"
            )
        self.assertEqual(result["matched_name"], "Pasta, dry, wholewheat, raw")
        self.assertEqual(result["selected_alternative"], "wholewheat penne pasta")

    def test_and_compound_is_not_replaced_by_one_component(self):
        p1, p2 = self._patch_pools(
            irish=[_cand("Pepper black/white", 0.05), _cand("Salt", 0.10)],
        )
        with p1, p2:
            result = nm.best_nutrition_match("salt and pepper", "irish")
        self.assertIsNone(result["match"])
        self.assertEqual(result["reason"], "compound_ingredient_requires_split")
        self.assertIn("separate ingredient weights", result["nutrition_match_note"])

    def test_unmatched_negligible_seasonings_are_intentionally_zeroed(self):
        for query in ("chilli flakes", "Italian seasoning", "sumac"):
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(
                    irish=[_cand("Pepper, capsicum, red, raw", 0.10)]
                )
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])
                self.assertEqual(
                    result["reason"], "intentional_negligible_seasoning"
                )
                self.assertTrue(result["intentionally_ignored"])

    def test_bare_chickpea_requires_a_preparation_state(self):
        for query in ("chickpea", "chick peas", "garbanzo beans"):
            with self.subTest(query=query):
                result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])
                self.assertEqual(result["reason"], "ambiguous_preparation_state")
                self.assertIn("dried, cooked, or canned", result["nutrition_match_note"])

    def test_exact_requested_form_beats_higher_ranked_generic_parent(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Paprika", 0.02),
                _cand("Paprika, smoked", 0.25),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("smoked paprika", "irish")
        self.assertEqual(result["matched_name"], "Paprika, smoked")
        self.assertEqual(result["reason"], "")

    def test_shared_form_word_cannot_replace_food_identity(self):
        wrong_matches = (
            ("vanilla paste", "Curry paste"),
            ("flour tortillas", "Flour corn"),
            ("basil pesto", "Basil fresh"),
            ("tomato passata", "Tomato, green, raw"),
            ("liquid honey", "Liquid caramel"),
            ("pumpkin pie spice", "Shepherd's pie, vegetable"),
            ("winter squash", "Carrot winter raw"),
            ("pearl couscous", "Barley pearl raw"),
            ("cream-style corn", "Cream cooking"),
            ("cream-style corn", "Cornetto type ice cream cone"),
        )
        for query, candidate in wrong_matches:
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])
                expected_reason = (
                    "intentional_negligible_seasoning"
                    if query == "pumpkin pie spice"
                    else "top_candidate_incompatible"
                )
                self.assertEqual(result["reason"], expected_reason)

    def test_unrequested_product_form_is_rejected(self):
        for query, candidate in (
            ("yoghurt", "Yoghurt drink"),
            ("yoghurt", "Yoghurt cream with fruit"),
            ("red chilli", "Ketchup hot chilli"),
            ("almonds", "Chocolate almonds"),
            ("sweet onion", "Sweet potato and onion layer"),
        ):
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])

    def test_confirmed_corpus_false_positives_are_rejected(self):
        wrong_matches = (
            ("butter", "Butter cream"),
            ("butter", "Ghee, butter"),
            ("chilli flakes", "Flakes spelt"),
            ("chilli flakes", "Chili pepper, raw"),
            ("reduced-fat spread", "Cheese spread, plain, reduced fat"),
            ("apple", "Apple turnover"),
            ("castor sugar", "Sugar castor brown"),
            ("corn", "Maize/corn bran"),
            ("kidney beans", "Rice and red kidney beans"),
            ("cream cheese", "Cheesecake made w cream cheese"),
            ("spaghetti", "Squash, spaghetti, raw"),
            ("peaches", "Jam, peach"),
            ("red pepper flakes", "Rye flakes"),
            ("vanilla essence", "Coffee and chicory essence, with water"),
            ("rice noodles", "Mix spice and herbs rice/Chinese noodles unprepared"),
            ("macaroni", "Macaroni cheese, homemade"),
            ("oatmeal", "Porridge oatmeal prepared w whole milk unsweetened"),
            ("fruit", "Mousse, fruit"),
            ("garlic cloves", "Cloves"),
            ("silver beet", "Beetroot, raw"),
            ("pistachios", "Tongue sausage with pistachios"),
            ("apricots", "Apricots tart"),
            ("whole wheat bread", "Wheat nut bread wholemeal"),
            ("puff pastry", "Cheese pastry w puff pastry"),
            ("seasoning", "Green salad, raw, without seasoning"),
            ("pear", "Pear nectar"),
            ("star anise", "Anise spirit, diluted"),
            ("coconut flakes", "Oat flakes"),
            ("ice-block sticks", "Foie gras block"),
            ("peppercorns", "Capsicum"),
            ("parsley root", "Ginger root"),
            ("passionfruit pulp", "Coconut pulp"),
            ("walnut halves", "Quark"),
            ("romaine hearts", "Pig heart"),
            ("white wine vinegar", "Red wine vinegar"),
            ("rice wine vinegar", "Red wine vinegar"),
            ("jasmine rice", "Wild rice"),
            ("lamb steak", "Beef steak"),
            ("bocconcini", "Meat balls"),
            ("onion jam", "Raspberry jam"),
            ("field greens", "Field mushroom"),
            ("apple", "Apple strudel"),
            ("corn", "Corn snacks"),
            ("spaghetti", "Spaghetti bolognese, homemade"),
            ("peaches", "Peach compote, prepacked"),
            ("scotch fillet steaks", "Scotch pancakes, homemade"),
            ("wheat berries", "Elderberry, berries, raw"),
            ("tahini dressing", "Dressing, yogurt, homemade"),
            ("teriyaki marinade", "Mussels in tomato marinade"),
            ("cavolo nero", "Mineral still water (Nero), bottled"),
            ("diced tomatoes with herbs", "Crouton with garlic and herbs"),
            ("ciabatta loaf", "Red pea loaf"),
            ("banana chips", "Maize/corn chips or tortilla chips, plain"),
            ("rosemary stems", "Rhubarb stems, stewed without sugar"),
            ("stock", "Ray, cooked in an aromatic stock"),
            ("chilli oil", "Anchovy fillets, marinated in chilli oil"),
            ("quorn mince", "Veal, mince, raw"),
            ("goji berries", "Elderberry, berries, raw"),
            ("red apple", "Apple fritter"),
            ("corn kernels", "Corn relish"),
            ("quorn mince", "Mince pies, retail"),
            ("muesli cereal", "Yoghurt with muesli"),
            ("asian green vegetables", "Hummus with vegetables"),
            ("fresh low salt tomato pasta sauce", "Baked beans in tomato sauce"),
            ("raisin bran", "Muffins, bran, homemade"),
            ("tahini dressing", "French dressing"),
            ("dumpling skins", "Potatoes, without skin"),
            ("beetroot vacuum pack", "Tomato, sieved, pack"),
            ("beetroot vacuum pack", "Potato, steamed, vacuum-packed"),
            ("green veg stir-fry mix", "Thai curry stir fry vegetables"),
            ("dark-choc bits", "Breakfast cereal Spec K choc Kellogg's"),
            ("za'atar spice", "Chinese 5 spice"),
            ("poultry seasoning", "Mix seasoning Mexican unprepared"),
            ("olive spray oil", "Olive (average)"),
            ("paprika spray oil", "Paprika"),
            ("spaghetti noodles", "Noodles instant prepared"),
            ("green peas", "Spring vegetables, frozen, raw (green peas, potatoes, carrots)"),
            ("mashed banana", "Plantain banana, cooked"),
            ("skinless salmon fillets", "Salmon, canned in brine, skinless and boneless"),
            ("fettuccine noodles", "Soup, chicken noodle, dried"),
            ("corn sweetcorn tortillas", "Tortilla, wheat, soft"),
            ("peach juice", "Peaches, canned in juice, whole contents"),
            ("wholegrain bread", "Rye bread wholemeal"),
            ("tart cherries", "Cake, cherry, homemade"),
            ("scone mix", "Scones, potato, homemade"),
            ("cooked fish", "Fish, breaded, fried, prepacked"),
            ("cooked fish", "Fish balls, steamed"),
        )
        for query, candidate in wrong_matches:
            with self.subTest(query=query, candidate=candidate):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertIsNone(result["match"])

    def test_new_guards_keep_legitimate_food_matches(self):
        good_matches = (
            ("butter", "Butter"),
            ("chilli powder", "Chili powder"),
            ("chilli flakes", "Chili pepper, dried, flakes"),
            ("cream cheese", "Cream cheese"),
            ("garlic cloves", "Garlic, raw"),
            ("jasmine rice", "Rice, jasmine, raw"),
            ("lamb steak", "Lamb steak, raw"),
            ("mozzarella balls", "Mozzarella"),
            ("oatmeal", "Oatmeal, raw"),
            ("white wine vinegar", "White wine vinegar"),
            ("caster sugar", "Sugar castor white"),
            ("wholegrain bread", "Bread rolls, wholemeal"),
            ("cornstarch", "Corn starch"),
            ("cornflour", "Corn starch"),
            ("sweet chilli sauce", "Chilli sauce"),
            ("silver beet", "Chard, Swiss, raw"),
            ("green veg stir-fry mix", "Vegetable stir fry mix, fried in rapeseed oil"),
            ("rice vermicelli noodles", "Rice vermicelli, dry, raw"),
            ("olive spray oil", "Olive oil"),
            ("easter eggs", "Milk chocolate"),
            ("chinese five spice", "Chinese 5 spice"),
            ("bran flakes", "Breakfast cereal, bran flakes, fortified"),
            ("cornflakes", "Breakfast cereal Cornflakes"),
            ("flour tortillas", "Tortilla, wheat, soft"),
            ("canola oil", "Oil, rapeseed"),
            ("passata", "Tomato puree"),
            ("onion powder", "Onion, dried"),
            ("liquid chicken stock", "Stock, chicken, ready made, retail"),
            ("tuna in spring water", "Tuna in water tinned"),
            ("flaked almonds", "Almonds, flaked and ground"),
            ("star anise", "Anise seed"),
            ("dark soy sauce", "Soy sauce, light and dark varieties"),
            ("vanilla essence", "Vanilla, aqueous extract"),
            ("oatmeal", "Oat, raw"),
            ("goji berries", "Goji berries, dried"),
            ("low-fat milk", "Milk, 1% fat, pasteurised"),
            ("applesauce", "Apple sauce, homemade"),
            ("instant yeast", "Yeast dried"),
            ("tamari", "Soy sauce"),
            ("tamari almonds", "Almonds, whole kernels"),
            ("jalapeno pepper", "Chili pepper, raw"),
            ("heavy cream", "Cream, fresh, whipping"),
            ("prosciutto", "Dry-cured ham"),
            ("almond meal", "Flour almond"),
            ("mandarin oranges", "Clementine or Mandarin orange, raw"),
            ("harissa", "Harissa hot spicy sauce"),
            ("refried beans", "Re-fried beans"),
            ("baked beans", "Baked beans, canned in tomato sauce"),
            ("sun-dried tomato pesto", "Pesto red"),
            ("portobello mushrooms", "Mushroom, all types, raw"),
            ("garlic-infused olive oil", "Olive oil"),
            ("jasmine rice", "Rice, white, long grain, raw"),
            ("soy milk", "Soy drink, prepacked (average)"),
            ("kecap manis", "Soy sauce sweet Ketjap"),
            ("vegetable stock powder", "Stock powder"),
            ("chicken stock powder", "Stock powder"),
        )
        for query, candidate in good_matches:
            with self.subTest(query=query, candidate=candidate):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertEqual(result["matched_name"], candidate)
                self.assertIsNotNone(result["match"])

    def test_highest_ranked_compatible_candidate_wins(self):
        p1, p2 = self._patch_pools(
            irish=[
                _cand("Spaghetti bolognese, homemade", 0.01),
                _cand("Pasta, white, spaghetti, dried, raw", 0.20),
            ],
        )
        with p1, p2:
            result = nm.best_nutrition_match("spaghetti", "irish")
        self.assertEqual(
            result["matched_name"], "Pasta, white, spaghetti, dried, raw"
        )

    def test_generic_products_prefer_average_rows(self):
        cases = (
            ("oil", "Oil, palm", "Oil, vegetable, average"),
            ("cheese", "Cheese, Mascarpone", "Cheese (average)"),
        )
        for query, specific, average in cases:
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(
                    irish=[_cand(specific, 0.05), _cand(average, 0.12)],
                )
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertEqual(result["matched_name"], average)

    def test_inherent_food_forms_do_not_cause_false_rejection(self):
        for query, candidate in (
            ("cumin", "Cumin seed"),
            ("ground cumin", "Cumin seed"),
            ("tahini", "Tahini paste"),
            ("tomato paste", "Tomato paste, concentrated, canned"),
        ):
            with self.subTest(query=query):
                p1, p2 = self._patch_pools(irish=[_cand(candidate, 0.05)])
                with p1, p2:
                    result = nm.best_nutrition_match(query, "irish")
                self.assertEqual(result["matched_name"], candidate)

    def test_smoked_food_can_use_plain_parent_without_accepting_other_food(self):
        p1, p2 = self._patch_pools(irish=[_cand("Paprika", 0.05)])
        with p1, p2:
            result = nm.best_nutrition_match("smoked paprika", "irish")
        self.assertEqual(result["matched_name"], "Paprika")
        self.assertEqual(result["reason"], "safe_parent_fallback")

    def test_compound_food_requires_every_food_identity(self):
        for query, partial_candidate in (
            ("salt and pepper", "Pepper, red, raw"),
            ("almonds or peanuts", "Peanuts, unsalted"),
            ("parsley, sage and thyme", "Thyme, fresh"),
        ):
            with self.subTest(query=query):
                self.assertFalse(
                    nm._compound_identities_match(query, partial_candidate)
                )

    def test_prefix_is_not_food_identity(self):
        self.assertEqual(
            nm._identity_match_level(
                "cream-style corn", "Cornetto type ice cream cone"
            ),
            "incompatible",
        )

    def test_irregular_plural_peas_keeps_food_identity(self):
        self.assertEqual(
            nm._identity_match_level("snow peas", "Snow pea, raw"),
            "exact",
        )

    def test_compound_spellings_and_irregular_plurals_keep_identity(self):
        for query, candidate in (
            ("cornstarch", "Corn starch"),
            ("kiwifruit", "Kiwi fruit, green"),
            ("passionfruit", "Passion fruit"),
            ("avocados", "Avocado"),
            ("white fish fillets", "European whitefish, raw"),
            ("wholemeal pita breads", "Bread, pita"),
        ):
            with self.subTest(query=query):
                self.assertNotEqual(
                    nm._selected_match_level(query, nm.clean_query(query), candidate),
                    "incompatible",
                )

    def test_safe_parent_policy_is_product_and_source_specific(self):
        self.assertEqual(
            nm._identity_match_level("kosher salt", "Salt"), "safe_parent"
        )
        self.assertEqual(
            nm._identity_match_level("garlic salt", "Salt"), "incompatible"
        )

    def test_safe_equivalences_recover_common_table_labels(self):
        for query, candidate in (
            ("broccolini", "Broccoli, raw"),
            ("cornflour", "Corn starch"),
            ("silken tofu", "Tofu, silky, prepacked"),
            ("black peppercorns", "Pepper, black"),
            ("cumin powder", "Cumin seeds"),
            ("chia seeds", "Chia seeds dried"),
        ):
            with self.subTest(query=query):
                self.assertNotEqual(
                    nm._selected_match_level(query, nm.clean_query(query), candidate),
                    "incompatible",
                )

    def test_real_alternative_is_not_treated_as_synonym(self):
        self.assertFalse(
            nm._compound_identities_match(
                "chicken or turkey", "Chicken/turkey pieces, coated, baked"
            )
        )
        self.assertTrue(
            nm._compound_identities_match("zucchini/courgette", "Courgettes raw")
        )


class NutritionIdentityBoundaryTests(unittest.TestCase):
    def test_calculator_passes_identity_separately_from_match_name(self):
        no_match = {
            "match": None,
            "source_key": "irish",
            "similarity": None,
            "confidence": "none",
            "reason": "no_candidates",
            "matched_name": None,
        }
        with patch.object(calc, "best_nutrition_match", return_value=no_match) as match:
            calc.nutritional_tool_vector.invoke({
                "title": "test",
                "ingredient_names": ["cooked green lentils"],
                "ingredient_identity_names": ["lentils"],
                "weights": [100.0],
            })
        self.assertEqual(match.call_args.kwargs["identity_name"], "lentils")
        self.assertEqual(match.call_args.args[0], "cooked green lentils")


class CuratedAliasTests(unittest.TestCase):
    """The 2026-09-16 curation pass: known-bad EU matches pinned via alias."""

    _ALIAS_ROWS = [
        {"alias": "chicken breast fillets", "eu_food_id": "ciqual:36017",
         "hungarian_food_id": "HU00237", "food_name": "Chicken, breast, without skin, raw"},
        {"alias": "skim milk", "eu_food_id": "ciqual:19050",
         "food_name": "Milk, skimmed, UHT"},
    ]

    def _patch_alias_table(self, rows=None):
        nm._alias_index.cache_clear()
        return patch.object(nm, "load_pipeline_data", return_value=rows if rows is not None else self._ALIAS_ROWS)

    def tearDown(self):
        nm._alias_index.cache_clear()

    def test_raw_name_hits_alias_before_es_pool(self):
        # No hungarian_food_id curated for this region in the test row -> the
        # irish region call must fall to the EU-curated record.
        with self._patch_alias_table(), patch.object(
            nm, "get_nutrition_candidate_by_source_id",
            return_value=_cand("Chicken, breast, without skin, raw", 0.0),
        ), patch.object(
            nm, "query_irish_nutrition_candidates",
            side_effect=AssertionError("ES pool must not be queried on an alias hit"),
        ), patch.object(nm, "query_eu_nutrition_candidates", return_value=[]):
            r = nm.best_nutrition_match("chicken breast fillets", "irish")
        self.assertEqual(r["confidence"], "curated")
        self.assertEqual(r["source_key"], "eu")
        self.assertEqual(r["matched_name"], "Chicken, breast, without skin, raw")

    def test_regional_curated_id_wins_over_eu_for_its_own_region(self):
        # hungarian_food_id IS curated for this row -> a hungarian-region call
        # must use the regional record, not silently collapse to EU.
        def _fetch(collection, food_id):
            self.assertEqual(collection, "nutritional_ingredients_hungarian")
            self.assertEqual(food_id, "HU00237")
            return _cand("chicken breast", 0.0)

        with self._patch_alias_table(), patch.object(
            nm, "get_nutrition_candidate_by_source_id", side_effect=_fetch,
        ):
            r = nm.best_nutrition_match("chicken breast fillets", "hungarian")
        self.assertEqual(r["source_key"], "hungarian")
        self.assertEqual(r["confidence"], "curated")

    def test_raw_name_checked_before_cleaned_name(self):
        # clean_query would strip "skim" from "skim milk" -> "milk"; the raw
        # form must win so "skim milk" doesn't resolve to whole milk.
        with self._patch_alias_table(), patch.object(
            nm, "get_nutrition_candidate_by_source_id",
            return_value=_cand("Milk, skimmed, UHT", 0.0),
        ), patch.object(nm, "query_irish_nutrition_candidates", return_value=[]), \
                patch.object(nm, "query_eu_nutrition_candidates", return_value=[]):
            r = nm.best_nutrition_match("skim milk", "irish")
        self.assertEqual(r["matched_name"], "Milk, skimmed, UHT")

    def test_miss_falls_through_to_es_pool(self):
        with self._patch_alias_table(rows=[]), \
                patch.object(nm, "query_irish_nutrition_candidates",
                              return_value=[_cand("Carrot, raw", 0.1)]), \
                patch.object(nm, "query_eu_nutrition_candidates", return_value=[]):
            r = nm.best_nutrition_match("carrots", "irish")
        self.assertEqual(r["matched_name"], "Carrot, raw")

    def test_missing_alias_table_degrades_to_es_pool(self):
        nm._alias_index.cache_clear()
        with patch.object(nm, "load_pipeline_data", side_effect=KeyError("missing")), \
                patch.object(nm, "query_irish_nutrition_candidates",
                              return_value=[_cand("Carrot, raw", 0.1)]), \
                patch.object(nm, "query_eu_nutrition_candidates", return_value=[]):
            r = nm.best_nutrition_match("carrots", "irish")
        self.assertEqual(r["matched_name"], "Carrot, raw")

    def test_reviewed_common_defaults_use_curated_composition_rows(self):
        rows = [
            {"alias": "pepper", "eu_food_id": "cofid:13-880"},
            {"alias": "margarine", "eu_food_id": "cofid:12-500"},
            {"alias": "rice", "eu_food_id": "ciqual:9100"},
            {"alias": "spaghetti", "eu_food_id": "cofid:11-716"},
            {"alias": "bread", "eu_food_id": "ciqual:7000"},
            {"alias": "red lentils", "eu_food_id": "cofid:13-657"},
            {"alias": "dates", "eu_food_id": "cofid:14-394"},
            {"alias": "chicken thighs", "eu_food_id": "ciqual:36019"},
            {"alias": "mixed salad leaves", "eu_food_id": "cofid:15-648"},
            {"alias": "rice noodles", "eu_food_id": "ciqual:9900"},
            {"alias": "penne pasta", "eu_food_id": "cofid:11-716"},
            {"alias": "italian herbs", "eu_food_id": "cofid:13-884"},
            {"alias": "italian herb mix", "eu_food_id": "cofid:13-884"},
            {"alias": "sweet corn", "eu_food_id": "cofid:13-622"},
        ]
        expected = {
            "pepper": ("cofid:13-880", "Pepper, black"),
            "margarine": ("cofid:12-500", "Baking fat and margarine (75-90% fat), hard block"),
            "rice": ("ciqual:9100", "Rice, white, raw"),
            "spaghetti": ("cofid:11-716", "Pasta, white, dried, raw"),
            "bread": ("ciqual:7000", "Bread (average)"),
            "red lentils": ("cofid:13-657", "Lentils, red, split, dried, raw"),
            "dates": ("cofid:14-394", "Dates, dried, flesh and skin"),
            "chicken thighs": ("ciqual:36019", "Chicken high leg, meat, raw"),
            "mixed salad leaves": ("cofid:15-648", "Salad, green"),
            "rice noodles": ("ciqual:9900", "Rice vermicelli, dry, raw"),
            "penne pasta": ("cofid:11-716", "Pasta, white, dried, raw"),
            "italian herbs": ("cofid:13-884", "Mixed herbs, dried"),
            "italian herb mix": ("cofid:13-884", "Mixed herbs, dried"),
            "sweet corn": ("cofid:13-622", "Sweetcorn, kernels, raw"),
        }

        def _fetch(_collection, food_id):
            return _cand(expected_name_by_id[food_id], 0.0)

        expected_name_by_id = {food_id: name for food_id, name in expected.values()}
        with self._patch_alias_table(rows=rows), patch.object(
            nm, "get_nutrition_candidate_by_source_id", side_effect=_fetch,
        ), patch.object(
            nm, "query_irish_nutrition_candidates",
            side_effect=AssertionError("curated defaults must bypass retrieval"),
        ):
            for ingredient, (_food_id, matched_name) in expected.items():
                with self.subTest(ingredient=ingredient):
                    result = nm.best_nutrition_match(ingredient, "irish")
                    self.assertEqual(result["confidence"], "curated")
                    self.assertEqual(result["matched_name"], matched_name)

    def test_ambiguous_generic_foods_abstain_before_retrieval(self):
        with self._patch_alias_table(rows=[]), patch.object(
            nm, "query_irish_nutrition_candidates",
            side_effect=AssertionError("ambiguous generic foods must not be retrieved"),
        ):
            for ingredient in ("beans", "broth", "stock"):
                with self.subTest(ingredient=ingredient):
                    result = nm.best_nutrition_match(ingredient, "irish")
                    self.assertIsNone(result["match"])
                    self.assertEqual(result["reason"], "ambiguous_food_identity")

    def test_prepared_stock_aliases_use_verified_liquid_rows(self):
        rows = [
            {"alias": "vegetable stock", "eu_food_id": "fineli:29026"},
            {"alias": "vegetable broth", "eu_food_id": "fineli:29026"},
            {"alias": "beef stock", "eu_food_id": "frida:534"},
            {"alias": "beef broth", "eu_food_id": "frida:534"},
        ]
        names = {
            "fineli:29026": "Vegetable bouillon, dissolved",
            "frida:534": "Bouillon, beef, cube, prepared",
        }

        def _fetch(collection, food_id):
            self.assertEqual(collection, "nutritional_ingredients_eu")
            return _cand(names[food_id], 0.0)

        with self._patch_alias_table(rows=rows), patch.object(
            nm, "get_nutrition_candidate_by_source_id", side_effect=_fetch,
        ):
            for ingredient, expected_id in (
                ("vegetable stock", "fineli:29026"),
                ("vegetable broth", "fineli:29026"),
                ("beef stock", "frida:534"),
                ("beef broth", "frida:534"),
            ):
                with self.subTest(ingredient=ingredient):
                    result = nm.best_nutrition_match(ingredient, "irish")
                    self.assertEqual(result["confidence"], "curated")
                    self.assertEqual(result["matched_name"], names[expected_id])

    def test_reduced_salt_stock_does_not_use_regular_stock_alias(self):
        with self._patch_alias_table(rows=[
            {"alias": "vegetable stock", "eu_food_id": "fineli:29026"},
        ]), patch.object(
            nm, "get_nutrition_candidate_by_source_id",
            side_effect=AssertionError("regular stock row must not be fetched"),
        ):
            result = nm.best_nutrition_match(
                "low-sodium vegetable stock", "irish", identity_name="vegetable stock"
            )
        self.assertIsNone(result["match"])
        self.assertEqual(result["reason"], "unsupported_nutrition_variant")

    def test_reduced_sodium_soy_does_not_use_regular_soy(self):
        with self._patch_alias_table(rows=[]), patch.object(
            nm, "query_irish_nutrition_candidates",
            side_effect=AssertionError("regular soy sauce must not be retrieved"),
        ):
            for ingredient in ("low-sodium soy sauce", "reduced-sodium soy sauce"):
                with self.subTest(ingredient=ingredient):
                    result = nm.best_nutrition_match(ingredient, "irish")
                    self.assertIsNone(result["match"])
                    self.assertEqual(result["reason"], "unsupported_nutrition_variant")

    def test_pickled_form_does_not_match_fresh_form(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "pickled ginger", "pickled ginger", "Ginger, fresh"
            )
        )

    def test_kaffir_lime_leaves_do_not_match_lime_fruit(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "kaffir lime leaves", "kaffir lime leaves", "Lime"
            )
        )

    def test_plain_pasta_does_not_invent_gluten_free_state(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "penne pasta", "penne pasta", "Pasta, dry, gluten-free, raw"
            )
        )
        self.assertTrue(
            nm._selected_match_is_compatible(
                "gluten-free penne pasta",
                "gluten-free penne pasta",
                "Pasta, dry, gluten-free, raw",
            )
        )

    def test_match_does_not_invent_cooked_state(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "wholemeal couscous", "wholemeal couscous", "Couscous wholemeal boiled"
            )
        )
        self.assertTrue(
            nm._selected_match_is_compatible(
                "cooked brown rice", "cooked brown rice", "Rice brown boiled"
            )
        )

    def test_low_sodium_query_does_not_use_regular_row(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "black beans",
                "reduced-sodium canned black beans",
                "Beans black canned",
            )
        )
        self.assertTrue(
            nm._selected_match_is_compatible(
                "kidney beans",
                "no-added-salt canned kidney beans",
                "Beans kidney red canned, no salt added",
            )
        )

    def test_bare_legumes_abstain_until_preparation_is_known(self):
        with self._patch_alias_table(rows=[]), patch.object(
            nm, "query_irish_nutrition_candidates",
            side_effect=AssertionError("ambiguous legumes must not be retrieved"),
        ):
            for ingredient in ("lentils", "black beans", "kidney beans"):
                with self.subTest(ingredient=ingredient):
                    result = nm.best_nutrition_match(ingredient, "irish")
                    self.assertIsNone(result["match"])
                    self.assertEqual(result["reason"], "ambiguous_preparation_state")

    def test_named_subtypes_do_not_collapse_to_wrong_food(self):
        self.assertFalse(
            nm._selected_match_is_compatible(
                "baby corn", "baby corn", "Sweetcorn boiled"
            )
        )
        self.assertFalse(
            nm._selected_match_is_compatible(
                "orange zest", "orange zest", "Lemon zest, raw"
            )
        )


if __name__ == "__main__":
    unittest.main()


class ReducedSaltStockCubeAliasTests(unittest.TestCase):
    def test_reduced_salt_cube_alias_is_used_but_unverified_low_sodium_stock_still_abstains(self):
        from recipe_wrangler.tools import nutrition_match as nm

        alias_rows = [{
            "alias": "low-salt chicken stock cube",
            "eu_food_id": "retail:knorr-fr-reduced-salt-chicken-cube",
        }]
        with patch.object(nm, "load_pipeline_data", return_value=alias_rows):
            hit = nm._curated_alias_lookup("low-salt chicken stock cube", "low salt chicken stock cube")
            self.assertEqual(hit["eu_food_id"], "retail:knorr-fr-reduced-salt-chicken-cube")
            self.assertIsNone(nm._curated_alias_lookup("low sodium vegetable stock", "low sodium vegetable stock"))
