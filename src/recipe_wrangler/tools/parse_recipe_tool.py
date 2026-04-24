# Purpose: LLM-based parser from raw recipe text to structured fields.

from typing import Any, List
import os
import re

from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

from recipe_wrangler.schemas import RecipeState


def _coerce_float(value: Any) -> float:
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


def _normalize_text(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 2:
        return token[:-1]
    return token


def _measurement_mentions_ingredient(measurement: str, ingredient: str) -> bool:
    measurement_norm = _normalize_text(measurement)
    ingredient_norm = _normalize_text(ingredient)
    if not measurement_norm or not ingredient_norm:
        return False

    if ingredient_norm in measurement_norm:
        return True

    measurement_tokens = set(measurement_norm.split())
    ingredient_tokens = [_singularize(tok) for tok in ingredient_norm.split()]
    return any(tok in measurement_tokens for tok in ingredient_tokens)


def _realign_measurements(
    ingredient_names: list[str],
    measurements: list[str],
) -> list[str]:
    """Realign measurements to ingredient order when parser output is clearly swapped."""
    if not ingredient_names or not measurements:
        return measurements

    n = min(len(ingredient_names), len(measurements))
    original = measurements[:n]
    assigned: list[str | None] = [None] * n
    used_measurement_indices: set[int] = set()

    # First pass: apply only high-confidence 1-to-1 matches.
    for ing_idx, ingredient in enumerate(ingredient_names[:n]):
        matches = [
            m_idx
            for m_idx, measurement in enumerate(original)
            if m_idx not in used_measurement_indices
            and _measurement_mentions_ingredient(measurement, ingredient)
        ]
        if len(matches) == 1:
            chosen = matches[0]
            assigned[ing_idx] = original[chosen]
            used_measurement_indices.add(chosen)

    # Second pass: fill remaining slots in original order.
    remaining = [
        original[m_idx] for m_idx in range(n) if m_idx not in used_measurement_indices
    ]
    rem_i = 0
    for ing_idx in range(n):
        if assigned[ing_idx] is None:
            assigned[ing_idx] = remaining[rem_i]
            rem_i += 1

    reordered = [m for m in assigned if m is not None]
    if reordered == original:
        return measurements

    # Preserve tail measurements (if parser returned more than ingredient count).
    return reordered + measurements[n:]

@tool
def parse_recipe_tool(recipe: str) -> dict:
    """Parses a raw recipe text into structured fields."""
    model_name = (os.getenv("PARSE_LLM") or "llama-3.1-8b-instant").strip()
    if model_name == "meta-llama/llama-4-maverick-17b-128e-instruct":
        # Legacy value kept in some environments; remap to a model we can serve.
        model_name = "llama-3.1-8b-instant"

    class ParsedRecipe(BaseModel):
        title: str = Field(default="Untitled recipe")
        ingredient_names: List[str] = Field(default_factory=list)
        measurements: List[str] = Field(default_factory=list)
        directions: List[str] = Field(default_factory=list)
        total_time: str = Field(default="0")
        serves: str = Field(default="0")

        @field_validator("title", mode="before")
        @classmethod
        def _default_title(cls, v: Any) -> str:
            text = (str(v).strip() if v is not None else "")
            return text or "Untitled recipe"

        @field_validator("total_time", "serves", mode="before")
        @classmethod
        def _stringify_number(cls, v: Any) -> str:
            if v is None:
                return "0"
            return str(v).strip() or "0"

    llm_source = os.getenv("WEIGHT_LLM_SOURCE", "groq").strip().lower()
    if llm_source == "vllm":
        base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8007/v1")
        api_key = os.getenv("VLLM_API_KEY", "none")
        llm = ChatOpenAI(model=model_name, temperature=0.0, max_retries=2, base_url=base_url, api_key=api_key)
        structured_method = "function_calling"
    else:
        llm = ChatGroq(model=model_name, temperature=0.0, max_retries=2)
        structured_method = os.getenv("PARSE_LLM_STRUCTURED_METHOD", "json_schema").strip() or "json_schema"

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "Parse this recipe into two index-aligned lists: 'ingredient_names' and 'measurements'.\n\n"
                "RULES FOR INGREDIENT NAMES:\n"
                "- Extract ONLY the core noun. Remove all descriptors, adjectives, and processing instructions "
                "(e.g., remove 'unsweetened', 'non-fat', 'all-purpose', 'canola', 'large', 'sifted').\n"
                "- Example: '1 cup applesauce, unsweetened' -> 'applesauce'.\n\n"
                "RULES FOR MEASUREMENTS:\n"
                "- Return a single numeric float followed by the unit (e.g., '2.5 tbsp', '1.0 cup').\n"
                "- If a range is given (e.g., '2-3 tbsp'), calculate the mean average (e.g., '2.5 tbsp').\n"
                "- If no unit is present (e.g., '2 eggs'), return only the number as a string (e.g., '2.0').\n"
                "- Convert all fractions to decimals (e.g., '1/2' becomes '0.5')."
            ),
            ("human", "Recipe: {recipe}"),
        ]
    )
    chain = prompt | llm.with_structured_output(ParsedRecipe, method=structured_method)
    try:
        result = chain.invoke({"recipe": recipe})
    except Exception as exc:
        # Fallback: try function_calling method if json_schema is not supported
        if structured_method == "json_schema" and "response format `json_schema`" in str(exc):
            fallback_chain = prompt | llm.with_structured_output(ParsedRecipe, method="function_calling")
            result = fallback_chain.invoke({"recipe": recipe})
        else:
            raise
    return result.model_dump()


def Recipe_Parser_Node(state: RecipeState) -> RecipeState:
    """
    Node that converts raw recipe text in state into structured fields 
    (title, ingredients, measurements, directions, total_time, serves).
    """
    debug = bool(state.debug)

    result = parse_recipe_tool.invoke({"recipe": state.raw_recipe})

    state.title = result["title"]
    state.ingredient_names = result["ingredient_names"]
    state.measurements = _realign_measurements(
        state.ingredient_names or [],
        result["measurements"] or [],
    )
    state.directions = result["directions"]
    state.total_time = _coerce_float(result.get("total_time"))
    state.serves = _coerce_int(result.get("serves"))

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", list(state.model_dump().keys()))
        
    return state
