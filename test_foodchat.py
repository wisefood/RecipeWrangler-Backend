import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from recipe_wrangler.api.main import app

client = TestClient(app)


class FoodChatEndpointTests(unittest.TestCase):
    @patch("recipe_wrangler.api.routers.recipes.fetch_foodchat_candidates")
    def test_foodchat_endpoint_returns_grouped_results(self, mock_fetch_foodchat_candidates):
        mock_fetch_foodchat_candidates.return_value = {
            "breakfast": [],
            "dinner": [
                {
                    "recipe_id": "42",
                    "title": "Chicken Bowl",
                    "ingredients": "1 chicken breast, 1 cup rice",
                    "directions": "Cook chicken. Serve over rice.",
                }
            ],
        }
        payload = {
            "user_profile": {
                "allergies": ["peanuts", "shellfish"],
                "diet": ["gluten_free", "mediterranean"],
            },
            "constraints": {
                "include_ingredients": ["chicken"],
                "exclude_ingredients": ["olives"],
                "exclude_recipe_ids": ["uuid-1234"],
            },
            "quotas": {
                "breakfast": 0,
                "dinner": 1,
            },
        }

        response = client.post("/api/v1/recipes/foodchat_candidates", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["results"]["dinner"][0]["recipe_id"], "42")
        self.assertEqual(body["results"]["dinner"][0]["title"], "Chicken Bowl")
        self.assertEqual(body["results"]["breakfast"], [])
        mock_fetch_foodchat_candidates.assert_called_once()


if __name__ == "__main__":
    unittest.main()
