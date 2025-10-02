from typing import TypedDict, List, Dict, Any
import dspy
from langchain.tools import tool
import core.lm_config
from langchain.tools import tool

from ollama import chat
import ast
import re



@tool
def ingredient_weight_tool_open(ingredient_names: Any, measurements: Any) -> dict:
    """
    Calculates the estimated weights for each ingredient in a recipe based on ingredient names and measurements.
    """

    system_prompt = (
        "You are an expert in estimating food ingredient weights in grams. "
        "You will receive two lists: one with ingredient names, and another with measurements. "
        "Return ONLY a list of numbers representing the estimated weights in grams of each ingredient in the order given. "
        "Return nothing but a valid JSON list of numbers, like [100, 5, 7]."
    )

    # Normalize inputs to robust lists of strings
    def _as_list(x: Any) -> list:
        if x is None:
            return []
        # Handle pandas/numpy NaN
        try:
            import math
            if isinstance(x, float) and math.isnan(x):
                return []
        except Exception:
            pass
        if isinstance(x, list):
            return x
        if isinstance(x, str):
            try:
                val = ast.literal_eval(x)
                if isinstance(val, list):
                    return val
            except Exception:
                # fall back to comma-separated parsing
                return [s.strip() for s in x.split(",") if s.strip()]
            return [x]
        # Try to coerce iterables (e.g., pandas Series) to list
        try:
            return list(x)
        except Exception:
            return [x]

    names_list = [str(v) for v in _as_list(ingredient_names)]
    measures_list = [str(v) for v in _as_list(measurements)]

    # Format user input for clarity
    user_input = f"Ingredients: {names_list}\nMeasurements: {measures_list}"

    response = chat(
        model='qwen3:8b',
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_input},
        ],
        think=False,  # disable reasoning tokens / <thinking> output
    )
    content = response['message']['content']
    
    return ast.literal_eval(content)

    
def Ingredient_Weight_Node(state: dict) -> dict:
    debug = state.get("debug", False)

    names = state.get("ingredient_names") or []
    measurements = state.get("measurements") or []

    # Fast-path: if measurements look like grams, bypass the model
    weights_g: list[float] = []
    all_grams_detected = True
    import re
    gram_pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:g|gram|grams)\b", re.IGNORECASE)

    for m in measurements:
        if isinstance(m, (int, float)):
            weights_g.append(float(m))
        elif isinstance(m, str):
            m_str = m.strip()
            # direct numeric string
            try:
                val = float(m_str)
                weights_g.append(val)
                continue
            except Exception:
                pass
            # patterns like "30 g", "5g"
            mt = gram_pattern.match(m_str)
            if mt:
                try:
                    weights_g.append(float(mt.group(1)))
                    continue
                except Exception:
                    pass
            all_grams_detected = False
            break
        else:
            all_grams_detected = False
            break

    if all_grams_detected and weights_g:
        n = min(len(names), len(weights_g))
        state.update({"weights": [float(x) for x in weights_g[:n]]})
        if debug:
            print("\n[Ingredient_Weight_Node] Bypassed model; using gram measurements directly.")
            print("[Ingredient_Weight_Node] Updated State Keys:", state.keys())
        return state

    # Otherwise, coerce to strings and call the tool
    measurements_str = [str(x) for x in measurements]
    result = ingredient_weight_tool_open.invoke({
        "ingredient_names": names,
        "measurements": measurements_str,
    })

    state.update({"weights": result})

    if debug:
        print("\n[Ingredient_Weight_Node] Used model to estimate weights.")
        print("[Ingredient_Weight_Node] Updated State Keys:", state.keys())

    return state
