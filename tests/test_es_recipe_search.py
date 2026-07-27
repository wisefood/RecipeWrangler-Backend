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
        patch.object(
            search,
            "post_query_with_retry",
            return_value=response,
        ) as post,
    ):
        result = search.search_recipes_es(search.RecipeSearchConstraints())

    assert result["total"] == 0
    assert post.call_args.args[0] == "http://elastic:9200/recipes_v2/_search"


def test_recipe_search_still_requires_profiled_recipes():
    payload = search.build_es_query(search.RecipeSearchConstraints())

    assert {"exists": {"field": "nutri_score_eu"}} in payload["query"]["bool"]["filter"]


def test_title_search_combines_exact_phrase_prefix_and_fuzzy_matching():
    payload = search.build_es_query(
        search.RecipeSearchConstraints(title_query="Chicken Tikka Masala")
    )

    bool_query = payload["query"]["bool"]
    clauses = bool_query["should"]
    assert bool_query["minimum_should_match"] == 1
    assert clauses[0] == {
        "term": {
            "title_normalized": {
                "value": "chicken tikka masala",
                "boost": 100,
            }
        }
    }
    assert any("match_phrase" in clause for clause in clauses)
    assert any("match_phrase_prefix" in clause for clause in clauses)
    assert any(
        clause.get("match", {}).get("title", {}).get("fuzziness") == "AUTO"
        for clause in clauses
    )
    assert payload["sort"][0] == "_score"


def test_title_normalization_ignores_case_accents_and_punctuation():
    assert search.normalize_recipe_title("  Čevapčići—Grilled! ") == "cevapcici grilled"


def test_title_search_reranks_fuzzy_candidates_by_complete_title_similarity():
    settings = SimpleNamespace(
        elastic_url="http://elastic:9200",
        elastic_index="recipes_v2",
        elastic_timeout=3.0,
    )
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "took": 1,
        "hits": {
            "total": {"value": 2},
            "hits": [
                {
                    "_score": 20,
                    "_source": {
                        "id": "long",
                        "title": "Chicken Soup for the Soul",
                    },
                },
                {
                    "_score": 10,
                    "_source": {
                        "id": "short",
                        "title": "Chicken Soup",
                    },
                },
            ],
        },
    }

    with (
        patch.object(search, "get_settings", return_value=settings),
        patch.object(
            search,
            "post_query_with_retry",
            return_value=response,
        ),
    ):
        result = search.search_recipes_es(
            search.RecipeSearchConstraints(
                title_query="Chiken Soup",
                limit=1,
            )
        )

    assert result["results"][0]["recipe_id"] == "short"


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


def test_natural_language_title_intent_uses_title_search_only():
    extractor = Mock()
    extractor.run_extract_constraints.return_value = {
        "query_constraints": {
            "search_intent": "title",
            "title_query": "chicken tikka masala",
            # These should be ignored for a title-only decision.
            "preferred_ingredients": ["chicken"],
            "excluded_ingredients": ["milk"],
            "allergens": ["milk"],
            "diet": ["vegan"],
            "title_keywords": ["tikka"],
            "max_duration_minutes": 30,
            "min_servings": 4,
            "limit": 10,
        }
    }

    with (
        patch.object(
            recipes,
            "get_recipe_constraint_extractor",
            return_value=extractor,
        ),
        patch.object(
            recipes,
            "search_recipes_es",
            return_value={"results": []},
        ) as es_search,
    ):
        asyncio.run(
            recipes.recipe_search(
                RecipeSearchRequest(question="Chicken Tikka Masala")
            )
        )

    constraints = es_search.call_args.args[0]
    assert constraints.title_query == "chicken tikka masala"
    assert constraints.include_ingredients == []
    assert constraints.exclude_ingredients == []
    assert constraints.diet_tags == []
    assert constraints.max_duration_minutes is None


def test_natural_language_mixed_intent_combines_title_and_filters():
    extractor = Mock()
    extractor.run_extract_constraints.return_value = {
        "query_constraints": {
            "search_intent": "title_with_constraints",
            "title_query": "chicken tikka masala",
            "preferred_ingredients": [],
            "excluded_ingredients": ["milk"],
            "allergens": ["milk"],
            "diet": [],
            "title_keywords": [],
            "max_duration_minutes": 30,
            "min_servings": None,
            "limit": 10,
        }
    }

    with (
        patch.object(
            recipes,
            "get_recipe_constraint_extractor",
            return_value=extractor,
        ),
        patch.object(
            recipes,
            "search_recipes_es",
            return_value={"results": []},
        ) as es_search,
    ):
        asyncio.run(
            recipes.recipe_search(
                RecipeSearchRequest(
                    question="Chicken Tikka Masala under 30 minutes"
                )
            )
        )

    constraints = es_search.call_args.args[0]
    assert constraints.title_query == "chicken tikka masala"
    assert constraints.exclude_ingredients == ["milk"]
    assert constraints.exclude_allergens == ["milk"]
    assert constraints.max_duration_minutes == 30
