"""A member's likes and dislikes reach both search paths.

The parameter search had no field for liked foods although its Elasticsearch
builder already ranks `boost_ingredients` (the `elif should:` sort branch), and
the question search had no field for disliked foods although the builder
already excludes `exclude_ingredients`. The UI therefore sent neither, and a
profile full of preferences produced the same page as an empty one.
"""

from recipe_wrangler.schemas.models import RecipeSearchFilters, RecipeSearchRequest
from recipe_wrangler.tools.es_recipe_search import RecipeSearchConstraints


def test_parameter_search_carries_liked_foods():
    filters = RecipeSearchFilters(preferred_ingredients=["Chicken", "broccoli"])
    assert filters.preferred_ingredients == ["Chicken", "broccoli"]
    assert RecipeSearchFilters().preferred_ingredients == []


def test_question_search_carries_disliked_foods():
    request = RecipeSearchRequest(question="dinner", exclude_ingredients=["mushrooms"])
    assert request.exclude_ingredients == ["mushrooms"]
    assert RecipeSearchRequest().exclude_ingredients == []


def test_the_constraints_the_routes_build_have_both_knobs():
    constraints = RecipeSearchConstraints(
        boost_ingredients=["chicken"], exclude_ingredients=["mushrooms"]
    )
    assert constraints.boost_ingredients == ["chicken"]
    assert constraints.exclude_ingredients == ["mushrooms"]
