import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from recipe_wrangler.api.routers import recipes
from recipe_wrangler.schemas import RecipeSearchRequest
from recipe_wrangler.tools import es_recipe_search as search


def test_recipe_search_uses_configured_index():
    settings = SimpleNamespace(
        elastic_url="http://elastic:9200",
        elastic_index="recipes_v2",
        elastic_timeout=3.0,
    )
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "took": 1,
        "hits": {"total": {"value": 0}, "hits": []},
    }

    with (
        patch.object(search, "get_settings", return_value=settings),
        patch.object(search.requests, "post", return_value=response) as post,
    ):
        result = search.search_recipes_es(search.RecipeSearchConstraints())

    assert result["total"] == 0
    assert post.call_args.args[0] == "http://elastic:9200/recipes_v2/_search"


def test_recipe_search_still_requires_profiled_recipes():
    payload = search.build_es_query(search.RecipeSearchConstraints())

    assert {"exists": {"field": "nutri_score_eu"}} in payload["query"]["bool"]["filter"]


def test_natural_language_search_always_uses_elasticsearch():
    extractor = Mock()
    extractor.run_extract_constraints.return_value = {
        "query_constraints": {
            "preferred_ingredients": ["lentils"],
            "excluded_ingredients": [],
            "allergens": [],
            "diet": ["vegan"],
            "title_keywords": [],
            "max_duration_minutes": 30,
            "min_servings": None,
            "limit": 10,
        }
    }
    es_result = {
        "results": [
            {
                "recipe_id": "recipe-1",
                "title": "Lentil stew",
                "nutri_color": "green",
            }
        ]
    }

    with (
        patch.object(
            recipes,
            "get_recipe_constraint_extractor",
            return_value=extractor,
        ),
        patch.object(recipes, "search_recipes_es", return_value=es_result) as es_search,
    ):
        result = asyncio.run(
            recipes.recipe_search(
                RecipeSearchRequest(question="vegan lentils under 30 minutes")
            )
        )

    assert result["results"][0]["recipe_id"] == "recipe-1"
    extractor.run_extract_constraints.assert_called_once_with(
        "vegan lentils under 30 minutes"
    )
    constraints = es_search.call_args.args[0]
    assert constraints.include_ingredients == ["lentils"]
    assert constraints.diet_tags == ["vegan"]
    assert constraints.max_duration_minutes == 30
