from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from langchain_core.runnables import RunnableLambda

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
        max_tokens=parser._MAX_TOKENS,
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


class TestCompletionBudget:
    """A long recipe used to 503.

    Groq's json_schema mode must emit the whole document inside the completion
    budget, and gpt-oss-20b spends part of that budget thinking. With no cap the
    provider applied its own, returned 400 json_validate_failed, and its SDK
    does not retry a 400 — so POST /recipes/profile failed outright on recipes
    that were merely long.
    """

    def test_groq_client_is_given_an_explicit_budget(self, monkeypatch):
        monkeypatch.setenv("WEIGHT_LLM_SOURCE", "groq")
        with patch.object(parser, "ChatGroq", return_value=Mock()) as chat:
            parser._parser_llm("openai/gpt-oss-20b")
        assert chat.call_args.kwargs["max_tokens"] == parser._MAX_TOKENS

    def test_the_budget_is_bigger_than_the_single_number_tool_needs(self):
        # `ingredient_weight_llm_tool` caps at 2048 to return one number. This
        # tool returns three index-aligned lists plus directions, so inheriting
        # that figure would reintroduce the bug it is meant to fix.
        assert parser._MAX_TOKENS >= 4096

    def test_the_budget_is_overridable(self, monkeypatch):
        monkeypatch.setenv("WEIGHT_LLM_SOURCE", "groq")
        with patch.object(parser, "ChatGroq", return_value=Mock()) as chat:
            parser._parser_llm("openai/gpt-oss-20b", max_tokens=1234)
        assert chat.call_args.kwargs["max_tokens"] == 1234

    @pytest.mark.parametrize(
        "message",
        [
            "Error code: 400 - {'error': {'code': 'json_validate_failed'}}",
            "max completion tokens reached before generating a valid document",
            "MAX COMPLETION TOKENS REACHED",
        ],
    )
    def test_budget_exhaustion_is_recognised(self, message):
        assert parser._is_budget_exhausted(Exception(message))

    @pytest.mark.parametrize(
        "message",
        [
            "response format `json_schema` is not supported",
            "connection reset by peer",
            "rate limit exceeded",
            "missing properties: 'directions'",
        ],
    )
    def test_other_failures_are_not_mistaken_for_it(self, message):
        # Misclassifying these would spend a second, wider call on an error a
        # bigger budget cannot fix.
        assert not parser._is_budget_exhausted(Exception(message))

    def _llm_that(self, *behaviours):
        """A fake parser LLM per call to `_parser_llm`, each with its own outcome.

        `with_structured_output` has to return a real Runnable: the tool composes
        it with `prompt | ...`, which a bare Mock cannot satisfy.
        """
        llms = []
        for behaviour in behaviours:
            llm = Mock()
            llm.with_structured_output.return_value = RunnableLambda(behaviour)
            llms.append(llm)
        return llms

    def test_a_long_recipe_retries_wider_instead_of_failing(self):
        parsed = Mock()
        parsed.model_dump.return_value = {"title": "Stew"}

        def over_budget(_):
            raise Exception(
                "Error code: 400 - {'error': {'code': 'json_validate_failed', "
                "'failed_generation': 'max completion tokens reached before "
                "generating a valid document'}}"
            )

        narrow, wide = self._llm_that(over_budget, lambda _: parsed)
        with patch.object(
            parser, "_parser_llm",
            side_effect=[(narrow, "json_schema"), (wide, "json_schema")],
        ) as build:
            result = parser.parse_recipe_tool.invoke({"recipe": "a long recipe"})

        assert result == {"title": "Stew"}
        # The retry must actually widen the budget, not just try again.
        assert build.call_args_list[1].kwargs["max_tokens"] == parser._MAX_TOKENS * 2

    def test_a_second_overflow_falls_back_to_function_calling(self):
        parsed = Mock()
        parsed.model_dump.return_value = {"title": "Stew"}

        def over_budget(_):
            raise Exception("json_validate_failed")

        narrow, wide = self._llm_that(over_budget, over_budget)
        # The wider client answers only once it is asked via function_calling.
        wide.with_structured_output.side_effect = lambda _schema, method: (
            RunnableLambda(lambda _: parsed)
            if method == "function_calling"
            else RunnableLambda(over_budget)
        )
        with patch.object(
            parser, "_parser_llm",
            side_effect=[(narrow, "json_schema"), (wide, "json_schema")],
        ):
            result = parser.parse_recipe_tool.invoke({"recipe": "a very long recipe"})

        assert result == {"title": "Stew"}

    def test_an_unrelated_failure_is_not_retried(self):
        def boom(_):
            raise Exception("missing properties: 'directions'")

        (narrow,) = self._llm_that(boom)
        with patch.object(
            parser, "_parser_llm", side_effect=[(narrow, "json_schema")],
        ) as build:
            with pytest.raises(Exception, match="directions"):
                parser.parse_recipe_tool.invoke({"recipe": "x"})
        assert build.call_count == 1
