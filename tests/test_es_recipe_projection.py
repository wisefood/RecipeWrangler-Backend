"""Runtime recipes_v2 projection — create/PATCH must produce a full, fresh doc
keyed and shaped exactly like the offline index_recipes_v2.py builder."""

import unittest
from unittest.mock import patch

from recipe_wrangler.utils import es_recipe_projection as mod


_NEO4J_ROW = {
    "id": "abc123def4",
    "title": "  Lentil Soup ",
    "url": "https://example.org/lentil",
    "image_url": "https://img.example.org/l.jpg",
    "source": "FoodHero",
    "source_id": "urn:rcollection:foodhero",
    "duration": 35,
    "serves": "4",
    "cost_category": "",
    "expert_recipe": True,
    "status": "active",
    "has_profile": True,
    "has_rcsi_nutrition": False,
    "has_planeat_nutrition": False,
    "ground_truth_nutrition_source": "",
    "ingredients": ["Lentils", "lentils", " Carrot "],
    "allergens": ["Celery"],
    "tags": ["gluten_free", "Main-Dish"],
    "dish_types": ["main-dish"],
}

_SCORES = {
    "sust_score": 1.25,
    "eu": {"nutri_score": "Nutriscore_B", "nutri_color": "green"},
    "ie": {"nutri_score": "Nutriscore_C", "nutri_color": "yellow"},
}


class BuildDocTests(unittest.TestCase):
    def test_assembles_full_doc_matching_builder_shape(self):
        with patch.object(mod, "run_query", return_value=[_NEO4J_ROW]), \
             patch.object(mod, "fetch_recipe_region_scores", return_value=_SCORES):
            doc = mod.build_recipe_v2_doc("abc123def4")

        self.assertEqual(doc["id"], "abc123def4")
        self.assertEqual(doc["title"], "Lentil Soup")
        self.assertEqual(doc["source_rank"], 0)  # foodhero is curated
        self.assertEqual(doc["ingredients"], ["lentils", "carrot"])  # deduped, lowered
        self.assertEqual(doc["dish_types"], ["main-dish"])
        self.assertEqual(doc["serves"], 4.0)
        self.assertIsNone(doc["cost_category"])
        self.assertEqual(doc["nutri_score_eu"], "Nutriscore_B")
        self.assertEqual(doc["nutri_color_ie"], "yellow")
        self.assertIsNone(doc["nutri_score_us"])
        self.assertEqual(doc["sust_score"], 1.25)
        self.assertEqual(doc["status"], "active")

    def test_unknown_recipe_returns_none(self):
        with patch.object(mod, "run_query", return_value=[]):
            self.assertIsNone(mod.build_recipe_v2_doc("nope"))


class ProjectTests(unittest.TestCase):
    def test_puts_full_doc_keyed_by_canonical_id(self):
        calls = {}

        class _Resp:
            def raise_for_status(self):
                pass

        def mock_put(url, json=None, timeout=None):
            calls["url"] = url
            calls["doc"] = json
            return _Resp()

        with patch.object(mod, "run_query", return_value=[_NEO4J_ROW]), \
             patch.object(mod, "fetch_recipe_region_scores", return_value=_SCORES), \
             patch.object(mod.requests, "put", side_effect=mock_put):
            ok = mod.project_recipe_to_es_v2(
                "abc123def4", es_url="http://es:9200", index="recipes_v2",
            )

        self.assertTrue(ok)
        self.assertEqual(calls["url"], "http://es:9200/recipes_v2/_doc/abc123def4")
        self.assertEqual(calls["doc"]["title"], "Lentil Soup")

    def test_missing_recipe_is_nonfatal(self):
        with patch.object(mod, "run_query", return_value=[]):
            ok = mod.project_recipe_to_es_v2(
                "ghost", es_url="http://es:9200", index="recipes_v2",
            )
        self.assertFalse(ok)

    def test_es_failure_is_nonfatal(self):
        with patch.object(mod, "run_query", return_value=[_NEO4J_ROW]), \
             patch.object(mod, "fetch_recipe_region_scores", return_value=_SCORES), \
             patch.object(mod.requests, "put", side_effect=ConnectionError("down")):
            ok = mod.project_recipe_to_es_v2(
                "abc123def4", es_url="http://es:9200", index="recipes_v2",
            )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
