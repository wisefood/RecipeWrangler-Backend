"""Adaptation provider selection and OpenRouter-specific request controls."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from recipe_wrangler.services.adaptation import contextual_assessment as contextual
from recipe_wrangler.services.adaptation import llm_judge as judge
from recipe_wrangler.services.adaptation.models import RoleAssessment
from recipe_wrangler.services.adaptation.schemas import SuggestionsResponse


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch):
    for key in ("ADAPT_LLM_SOURCE", "ADAPT_LLM_MODEL", "ADAPT_LLM_BASE_URL", "ADAPT_LLM_API_KEY",
                "ADAPT_LLM_TIMEOUT", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_openrouter_uses_qwen_slug_and_shared_key(monkeypatch):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = judge._llm_config()
    assert config == {"source": "openrouter", "model": "qwen/qwen3-32b", "api_key": "test-key",
                      "base_url": "https://openrouter.ai/api/v1", "timeout": 90}


def test_adaptation_overrides_take_precedence(monkeypatch):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "shared-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://shared.invalid/v1")
    monkeypatch.setenv("ADAPT_LLM_API_KEY", "adapt-key")
    monkeypatch.setenv("ADAPT_LLM_BASE_URL", "https://adapt.invalid/v1")
    monkeypatch.setenv("ADAPT_LLM_MODEL", "qwen/qwen3-32b")
    monkeypatch.setenv("ADAPT_LLM_TIMEOUT", "12")
    config = judge._llm_config()
    assert config["api_key"] == "adapt-key" and config["base_url"] == "https://adapt.invalid/v1"
    assert config["timeout"] == 12


def test_openrouter_shared_endpoint_override(monkeypatch):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://shared.invalid/v1")
    assert judge._llm_config()["base_url"] == "https://shared.invalid/v1"


@pytest.mark.parametrize("key", [None, "", " ", "none"])
def test_missing_router_credentials_fail_before_request(monkeypatch, key):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    if key is not None: monkeypatch.setenv("OPENROUTER_API_KEY", key)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        judge._llm_config()


@pytest.mark.parametrize("source,model,key", [("vllm", "qwen3-32b", "none"),
                                            ("groq", "llama-3.1-8b-instant", "groq-key")])
def test_existing_provider_defaults_are_preserved(monkeypatch, source, model, key):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", source)
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://router.invalid/v1")
    config = judge._llm_config()
    assert config["model"] == model and config["api_key"] == key
    assert "router.invalid" not in config["base_url"]


@pytest.mark.parametrize("legacy", [False, True])
def test_router_transport_sends_supported_reasoning_controls(monkeypatch, legacy):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-in-prompt")
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=json.dumps({"ranked": [{"substitute_name": "breast"}]})))])
    factory = MagicMock(return_value=client)
    client.__enter__.return_value = client
    monkeypatch.setattr(judge.openai, "OpenAI", factory)
    if legacy:
        result = judge.rerank_with_llm(candidates=[{"substitute_name": "breast", "rank": 1, "source": "test"}], recipe_title="Test",
            recipe_ingredients=[], target_nutrient_label=None, target_points=None, offending_ingredient="thigh",
            offending_pct=50, mode="vegan")
        assert result is not None and result["source"] == "openrouter"
    else:
        contextual._request({"task": "test"}, RoleAssessment, judge._llm_config(), max_tokens=50)
    sent = client.chat.completions.create.call_args.kwargs
    assert sent["model"] == "qwen/qwen3-32b"
    assert sent["extra_body"] == {"reasoning": {"enabled": False}, "provider": {"require_parameters": True}}
    assert "chat_template_kwargs" not in sent["extra_body"]
    assert "test-key-never-in-prompt" not in json.dumps(sent)
    if not legacy:
        assert "ingredient:<position>" in sent["messages"][0]["content"]


def test_legacy_judge_preserves_fallback_when_key_missing(monkeypatch):
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    assert judge.rerank_with_llm(candidates=[{"substitute_name": "breast"}], recipe_title="Test",
        recipe_ingredients=[], target_nutrient_label=None, target_points=None, offending_ingredient="thigh",
        offending_pct=50) is None


def test_non_router_request_bodies_stay_provider_specific():
    assert judge._request_extra_body("vllm") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert judge._request_extra_body("groq") == {}


def test_existing_api_response_accepts_openrouter_metadata():
    response = SuggestionsResponse(recipe_id="synthetic", region="IE", mode="nutrition",
        suggestions=[], llm_used=True, llm_source="openrouter", llm_model="qwen/qwen3-32b")
    assert response.model_dump(mode="json")["llm_source"] == "openrouter"
