import unittest
from unittest.mock import patch

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.tools.param_search import (
    build_param_search_cypher,
    search_recipes_by_params,
)


class ParamSearchTests(unittest.TestCase):
    def test_dish_types_filter_uses_dish_type_tag_relationships(self):
        query, facet_query, params = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["Breakfast", "main-dish"])
        )

        self.assertIsNone(facet_query)
        self.assertEqual(params["dish_types"], ["breakfast", "main-dish"])
        self.assertIn("MATCH (r)-[:HAS_TAG]->(dt:Tag)", query)
        self.assertIn("dt.category = 'dish-type'", query)
        self.assertIn("toLower(dt.name) IN $dish_types", query)

    def test_dish_types_counts_as_constraint(self):
        query, _, _ = build_param_search_cypher(
            RecipeSearchFilters(dish_types=["snacks"], limit=5)
        )

        self.assertIn("WHERE EXISTS", query)
        self.assertIn("LIMIT $limit", query)

    def test_dish_type_alias_accepts_single_value(self):
        filters = RecipeSearchFilters.model_validate({"dish_type": "beverages"})
        query, _, params = build_param_search_cypher(filters)

        self.assertEqual(params["dish_types"], ["beverages"])
        self.assertIn("dt.category = 'dish-type'", query)

    def test_sources_are_filterable_and_facetable(self):
        query, facet_query, params = build_param_search_cypher(
            RecipeSearchFilters(
                source="SafeFood",
                include_facets=True,
            )
        )

        self.assertEqual(params["sources"], ["irish_safefood"])
        self.assertIn("IN $sources", query)
        self.assertIsNotNone(facet_query)
        self.assertIn("RETURN 'source' AS category", facet_query)

    def test_preferred_sources_have_explicit_priority_order(self):
        query, _, _ = build_param_search_cypher(RecipeSearchFilters(limit=5))

        self.assertIn('= "healthyfoods" THEN 0', query)
        self.assertIn('= "foodhero" THEN 1', query)
        self.assertIn('= "myplate" THEN 2', query)
        self.assertIn('= "irish_safefood" THEN 3', query)
        self.assertIn('= "recipe1m" THEN 5', query)
        self.assertIn('ELSE 4', query)

    def test_unconstrained_browse_can_return_source_facets(self):
        # Three queries fire concurrently: results, count, facets. Route each
        # mock response by query shape so call ordering doesn't matter.
        def mock_run_query(query, _params):
            if "RETURN count(r) AS total" in query:
                return [{"total": 1}]
            if "RETURN 'source' AS category" in query:
                return [
                    {"category": "source", "tag": "healthyfoods", "count": 3},
                    {"category": "dish-type", "tag": "breakfast", "count": 2},
                ]
            return [{"recipe_id": "1", "title": "A"}]

        with patch(
            "recipe_wrangler.tools.param_search.run_query",
            side_effect=mock_run_query,
        ) as mock_run:
            result = search_recipes_by_params(RecipeSearchFilters(include_facets=True))

        self.assertEqual(result["facets"]["source"]["healthyfoods"], 3)
        self.assertEqual(result["facets"]["dish-type"]["breakfast"], 2)
        self.assertEqual(result["total"], 1)
        # Every call must have passed through the unconstrained-browse predicate.
        for call in mock_run.call_args_list:
            self.assertIn("coalesce(r.has_profile, false) = true", call.args[0])


if __name__ == "__main__":
    unittest.main()
