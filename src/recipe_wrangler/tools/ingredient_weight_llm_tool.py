# Purpose: Estimate ingredient weight (grams) from parsed quantity/unit via Groq or vLLM.

from typing import Any
import os
import re

from langchain.tools import tool
from groq import Groq
import openai
from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_SYSTEM_PROMPT = (
    "You estimate the weight in grams of a cooking ingredient for a given quantity and unit. "
    "Internally: estimate the grams of ONE unit of that specific ingredient, then multiply by the quantity. "
    "Use realistic culinary reference weights, for example:\n"
    "1 garlic clove ~3 g; 1 large egg ~50 g; 1 medium onion ~110 g; 1 medium tomato ~120 g; 1 medium carrot ~60 g; 1 lemon/lime ~65 g\n"
    "1 cup flour ~125 g; 1 cup sugar ~200 g; 1 cup water or milk ~240 g; 1 cup uncooked rice ~185 g; 1 cup chopped herbs ~25 g\n"
    "1 tablespoon oil ~14 g; 1 tablespoon butter ~14 g; 1 teaspoon salt ~6 g; 1 teaspoon ground spice ~2 g; 1 pinch ~0.3 g\n"
    "1 slice bread ~30 g; 1 slice bacon ~14 g; 1 sheet phyllo ~18 g; 1 lasagne sheet ~100 g; 1 stick butter ~113 g\n"
    "1 (14.5 oz) can ~410 g; 1 (15 oz) can ~425 g; 1 (28 oz) can ~795 g; 1 stock cube ~10 g; 1 standard package/box ~340-450 g\n"
    "Never convert the unit name literally into grams - a 'clove', 'slice' or 'can' is not an ounce or a pound. "
    "If the quantity or unit is missing or vague, assume one sensible typical amount and still answer. "
    "Output ONLY the final number of grams - no words, no units, no JSON, no explanation."
)


def _user_prompt(ingredient: str, parsed_quantity: Any, parsed_unit: str) -> str:
    return (
        f"Ingredient: {ingredient}\n"
        f"Quantity: {parsed_quantity}\n"
        f"Unit: {parsed_unit}\n"
        "Output: only numeric grams."
    )


def _extract_grams(text: str) -> float:
    matches = _NUMBER_RE.findall(text or "")
    if not matches:
        raise ValueError(f"Could not parse numeric grams from response: {text!r}")
    # Prefer the last numeric token to avoid picking quantity echoes like "2 apples = 364".
    grams = float(matches[-1])
    if grams < 0:
        raise ValueError(f"Received negative grams: {grams}")
    return grams


def _call_groq(model_name: str, ingredient: str, parsed_quantity: Any, parsed_unit: str) -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set.")
    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        max_tokens=48,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(ingredient, parsed_quantity, parsed_unit)},
        ],
    )
    return (completion.choices[0].message.content or "").strip()


def _call_vllm(model_name: str, ingredient: str, parsed_quantity: Any, parsed_unit: str) -> str:
    base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8007/v1")
    api_key = os.getenv("VLLM_API_KEY", "none")
    client = openai.OpenAI(base_url=base_url, api_key=api_key)
    completion = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        max_tokens=48,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(ingredient, parsed_quantity, parsed_unit)},
        ],
    )
    return (completion.choices[0].message.content or "").strip()


@tool
def ingredient_weight_llm_tool(
    ingredient: str,
    parsed_quantity: Any,
    parsed_unit: str,
) -> float:
    """
    Estimate ingredient weight in grams from ingredient + parsed quantity + parsed unit.
    Returns only the numeric weight (grams).

    LLM source is selected via WEIGHT_LLM_SOURCE env var: "groq" (default) or "vllm".
    vLLM endpoint is configured via VLLM_BASE_URL (default: http://localhost:8003/v1).
    """
    model_name = os.getenv("WEIGHT_LLM", os.getenv("GUARDRAILS_MODEL", "llama-3.1-8b-instant"))
    if not model_name:
        raise ValueError("WEIGHT_LLM is not set and no fallback model is available.")

    source = os.getenv("WEIGHT_LLM_SOURCE", "groq").strip().lower()

    if source == "vllm":
        content = _call_vllm(model_name, ingredient, parsed_quantity, parsed_unit)
    else:
        content = _call_groq(model_name, ingredient, parsed_quantity, parsed_unit)

    return _extract_grams(content)
