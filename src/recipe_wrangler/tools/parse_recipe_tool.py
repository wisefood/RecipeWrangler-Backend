# Purpose: LLM-based parser from raw recipe text to structured fields.

from typing import Any, List
import os
import re

from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from recipe_wrangler.schemas import RecipeState


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


_SOURCE_MEASUREMENT_RE = re.compile(
    r"^\s*"
    r"(?P<qty>(?:\d+\s+)?\d+(?:\.\d+)?(?:/\d+)?|[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"\s*"
    r"(?P<unit>tablespoons?|tbsp\.?|teaspoons?|tsp\.?|cups?|ml|millilitres?|milliliters?|"
    r"g|grams?|kg|oz\.?|ounces?|cloves?|spears?|sprigs?|small|medium|large)\b"
    r"(?:\s+of\b)?",
    re.IGNORECASE,
)


def _recover_measurements_from_source(
    ingredient_names: list[str],
    measurements: list[str],
    recipe: str,
) -> list[str]:
    """Restore source units when the LLM parser returned a bare number."""
    source_lines = [line.strip() for line in str(recipe or "").splitlines() if line.strip()]
    recovered = list(measurements)

    for idx, name in enumerate(ingredient_names):
        if idx >= len(recovered):
            break
        current = str(recovered[idx] or "").strip()
        if not current or re.search(r"[a-zA-Z]", current):
            continue

        name_tokens = {
            _singularize(token)
            for token in re.findall(r"[a-zA-Z]+", str(name).lower())
            if len(token) > 2
        }
        if not name_tokens:
            continue

        for line in source_lines:
            line_tokens = {
                _singularize(token)
                for token in re.findall(r"[a-zA-Z]+", line.lower())
                if len(token) > 2
            }
            if not (name_tokens & line_tokens):
                continue
            match = _SOURCE_MEASUREMENT_RE.match(line)
            if match:
                recovered[idx] = f"{match.group('qty')} {match.group('unit')}".strip()
                break

    return recovered


@tool
def parse_recipe_tool(recipe: str) -> dict:
    """Parses a raw recipe text into structured fields."""
    model_name = (os.getenv("PARSE_LLM") or "llama-3.1-8b-instant").strip()
    if model_name == "meta-llama/llama-4-maverick-17b-128e-instruct":
        # Legacy value kept in some environments; remap to a model we can serve.
        model_name = "llama-3.1-8b-instant"

    class ParsedRecipe(BaseModel):
        title: str = Field(min_length=1)
        ingredient_names: List[str] = Field(min_length=1)
        measurements: List[str] = Field(min_length=1)
        directions: List[str] = Field(min_length=1)
        total_time: float = Field(ge=0)
        serves: int = Field(ge=0)

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
                "- Example: '1 cup applesauce, unsweetened' -> 'applesauce'.\n"
                "- Do not include recipe section headings, component names, or preparation group titles as "
                "ingredients when they are followed by the actual ingredients for that component. For example, "
                "exclude headings like 'For the sauce', 'Dressing', 'Salad', or 'Falafels' if the following "
                "lines list the sauce/dressing/salad/falafel ingredients.\n"
                "- A component name can be an ingredient only when it has its own quantity or is clearly used "
                "as an edible ingredient line, e.g. '4 falafels' or '200g falafel'.\n"
                "- If a source line has multiple ingredients collapsed together, split it into separate "
                "ingredient rows instead of returning the collapsed line as one ingredient.\n\n"
                "- Do not add optional ingredients mentioned only in the instructions or serving notes. "
                "For example, if the ingredient list does not contain olive oil, do not add olive oil just "
                "because an instruction says it can optionally be added.\n"
                "- Prefer the explicit ingredient list over the instructions. Use instructions only for "
                "directions, timing, and disambiguating preparation.\n\n"
                "RULES FOR MEASUREMENTS:\n"
                "- Return a single numeric float followed by the unit (e.g., '2.5 tbsp', '1.0 cup').\n"
                "- Preserve the original measurement unit. Do not drop units such as tablespoon, teaspoon, "
                "ml, g, clove, small, medium, or large.\n"
                "- For countable items with a size adjective, keep the size adjective in the measurement, "
                "e.g. '2 small onions' -> ingredient_name 'onions', measurement '2.0 small'.\n"
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
    state.measurements = _recover_measurements_from_source(
        state.ingredient_names or [],
        state.measurements or [],
        state.raw_recipe or "",
    )
    state.directions = result["directions"]
    state.total_time = result["total_time"]
    trusted_serves = getattr(state, "trusted_serves", None)
    state.serves = trusted_serves or result["serves"]

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", list(state.model_dump().keys()))
        
    return state
