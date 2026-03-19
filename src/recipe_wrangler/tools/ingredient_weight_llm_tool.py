# Purpose: Estimate ingredient weight (grams) from parsed quantity/unit via direct Groq API.

from typing import Any
import os
import re

from langchain.tools import tool
from groq import Groq
from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_grams(text: str) -> float:
    matches = _NUMBER_RE.findall(text or "")
    if not matches:
        raise ValueError(f"Could not parse numeric grams from response: {text!r}")
    # Prefer the last numeric token to avoid picking quantity echoes like "2 apples = 364".
    grams = float(matches[-1])
    if grams < 0:
        raise ValueError(f"Received negative grams: {grams}")
    return grams


@tool
def ingredient_weight_llm_tool(
    ingredient: str,
    parsed_quantity: Any,
    parsed_unit: str,
) -> float:
    """
    Estimate ingredient weight in grams from ingredient + parsed quantity + parsed unit.
    Returns only the numeric weight (grams).
    """
    model_name = os.getenv("WEIGHT_LLM", os.getenv("GUARDRAILS_MODEL", "llama-3.1-8b-instant"))
    if not model_name:
        raise ValueError("WEIGHT_LLM is not set and no fallback model is available.")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set.")

    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        max_tokens=48,
        messages=[
            {
                "role": "system",
                "content": (
                    "Estimate ingredient weight in grams. "
                    "Return only one number (grams), no words, no units, no JSON. "
                    "Never ask follow-up questions. If unit or quantity is missing/unclear, "
                    "use a reasonable default assumption and still return one numeric grams value."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ingredient: {ingredient}\n"
                    f"Quantity: {parsed_quantity}\n"
                    f"Unit: {parsed_unit}\n"
                    "Output: only numeric grams."
                ),
            },
        ],
    )

    content = (completion.choices[0].message.content or "").strip()
    return _extract_grams(content)
