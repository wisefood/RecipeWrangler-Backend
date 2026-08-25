import json
from unittest.mock import patch

import pytest

from recipe_wrangler.utils.recipe_url import (
    RecipeUrlError,
    _validate_public_url,
    parse_recipe_html,
)


def _html(recipe):
    return (
        '<html><script type="application/ld+json">'
        + json.dumps({"@context": "https://schema.org", "@graph": [recipe]})
        + "</script></html>"
    )


def test_parses_schema_org_recipe_without_an_llm():
    result = parse_recipe_html(
        _html(
            {
                "@type": "Recipe",
                "name": "Tomato soup",
                "recipeIngredient": ["2 tomatoes", "500 ml stock"],
                "recipeInstructions": [
                    {"@type": "HowToStep", "text": "Chop the tomatoes."},
                    {"@type": "HowToStep", "text": "Simmer for 20 minutes."},
                ],
                "prepTime": "PT10M",
                "cookTime": "PT20M",
                "recipeYield": "4 servings",
                "image": {"url": "https://example.com/soup.jpg"},
            }
        ),
        "https://example.com/tomato-soup",
    )
    assert result["duration"] == 30
    assert result["serves"] == 4
    assert result["missing_required_fields"] == []
    assert result["url"] == "https://example.com/tomato-soup"
    assert result["instructions"] == [
        "Chop the tomatoes.",
        "Simmer for 20 minutes.",
    ]


def test_missing_serves_and_duration_stay_missing_not_invented():
    result = parse_recipe_html(
        _html(
            {
                "@type": "Recipe",
                "name": "Toast",
                "recipeIngredient": ["bread"],
                "recipeInstructions": "Toast the bread.",
            }
        ),
        "https://example.com/toast",
    )
    assert result["serves"] is None
    assert result["duration"] is None
    assert result["missing_required_fields"] == ["duration", "serves"]


def test_ambiguous_yield_is_not_guessed():
    result = parse_recipe_html(
        _html(
            {
                "@type": "Recipe",
                "name": "Bread",
                "recipeIngredient": ["flour"],
                "recipeInstructions": "Bake.",
                "totalTime": "PT1H",
                "recipeYield": "2-3 loaves",
            }
        ),
        "https://example.com/bread",
    )
    assert result["serves"] is None


def test_local_and_private_urls_are_rejected():
    with pytest.raises(RecipeUrlError):
        _validate_public_url("http://localhost/recipe")
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 80))]):
        with pytest.raises(RecipeUrlError):
            _validate_public_url("http://example.test/recipe")
