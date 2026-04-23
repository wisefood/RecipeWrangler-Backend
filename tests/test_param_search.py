import unittest

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.tools.param_search import build_param_search_cypher


class ParamSearchTests(unittest.TestCase):
    def test_dish_types_filter_uses_dish_type_tag_relationships(self):
        query, params = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["Breakfast", "main-dish"])
        )

        self.assertEqual(params["dish_types"], ["breakfast", "main-dish"])
        self.assertIn("MATCH (r)-[:HAS_TAG]->(dt:Tag)", query)
        self.assertIn("dt.category = 'dish-type'", query)
        self.assertIn("toLower(dt.name) IN $dish_types", query)

    def test_dish_types_counts_as_constraint(self):
        query, _ = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["snacks"], limit=5)
        )

        self.assertIn("WHERE EXISTS", query)
        self.assertIn("LIMIT $limit", query)

    def test_dish_type_alias_accepts_single_value(self):
        filters = RecipeSearchFilters.model_validate({"dish_type": "beverages"})
        query, params = build_param_search_cypher(filters)

        self.assertEqual(params["dish_types"], ["beverages"])
        self.assertIn("dt.category = 'dish-type'", query)


if __name__ == "__main__":
    unittest.main()
