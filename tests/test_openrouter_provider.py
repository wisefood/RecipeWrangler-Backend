from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from recipe_wrangler.tools import ingredient_weight_llm_tool as weight
from recipe_wrangler.tools import parse_recipe_tool as parser


def test_parser_builds_openrouter_client(monkeypatch):
    monkeypatch.setenv("WEIGHT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://router.example/v1")
    client = Mock()

    with patch.object(parser, "ChatOpenAI", return_value=client) as chat:
        result, method = parser._parser_llm("openai/gpt-4o-mini")

    assert result is client
    assert method == "function_calling"
    chat.assert_called_once_with(
        model="openai/gpt-4o-mini",
        temperature=0.0,
        max_retries=2,
        base_url="https://router.example/v1",
        api_key="test-key",
    )


def test_parser_requires_openrouter_key(monkeypatch):
    monkeypatch.setenv("WEIGHT_LLM_SOURCE", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        parser._parser_llm("openai/gpt-4o-mini")


def test_parser_match_names_fall_back_only_when_unaligned():
    names = ["lentils", "bell pepper"]

    assert parser._aligned_match_names(
        names,
        ["cooked green lentils", "red bell pepper"],
    ) == ["cooked green lentils", "red bell pepper"]
    assert parser._aligned_match_names(names, ["lentils"]) == names
    assert parser._aligned_match_names(names, None) == names


def test_weight_fallback_calls_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://router.example/v1")
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="84"))]
    )
    client = Mock()
    client.chat.completions.create.return_value = completion

    with patch.object(weight.openai, "OpenAI", return_value=client) as openai_client:
        result = weight._call_openrouter(
            "openai/gpt-4o-mini",
            "carrot",
            2,
            "medium",
        )

    assert result == "84"
    openai_client.assert_called_once_with(
        base_url="https://router.example/v1",
        api_key="test-key",
    )
    assert client.chat.completions.create.call_args.kwargs["model"] == (
        "openai/gpt-4o-mini"
    )
