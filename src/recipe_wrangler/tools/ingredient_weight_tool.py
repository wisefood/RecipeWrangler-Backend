# Purpose: Estimate ingredient weights (grams) using USDA portion/weight data.

from typing import Any, List, Optional, Tuple

import re
from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.usda_nutrients_v1 import canonical_name_to_usda
from recipe_wrangler.utils.weigh_calculation_usda_ import (
    match_portion,
    weight_from_ingredient,
)


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
            import ast
            val = ast.literal_eval(x)
            if isinstance(val, list):
                return val
        except Exception:
            return [s.strip() for s in x.split(",") if s.strip()]
        return [x]
    try:
        return list(x)
    except Exception:
        return [x]


_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<qty>[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?(?:\s*-\s*[0-9]+(?:\.[0-9]+)?)?)\s*(?P<unit>.*)$"
)

_RANGE_QTY_RE = r"[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?"
_RANGE_MEASUREMENT_RE = re.compile(
    rf"^\s*(?P<q1>{_RANGE_QTY_RE})\s*(?:to|-|–|—)\s*(?P<q2>{_RANGE_QTY_RE})\s*(?P<rest>.*)$"
)

_UNICODE_FRACTIONS = {
    "½": "1/2",
    "⅓": "1/3",
    "⅔": "2/3",
    "¼": "1/4",
    "¾": "3/4",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

_WORD_QUANTITIES = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "half": 0.5,
    "quarter": 0.25,
    "couple": 2.0,
    "few": 3.0,
}

_COUNTABLE_NOUNS = {
    "clove": "clove",
    "cloves": "clove",
    "sprig": "sprig",
    "sprigs": "sprig",
    "leaf": "leaf",
    "leaves": "leaf",
    "stalk": "stalk",
    "stalks": "stalk",
    "stick": "stick",
    "sticks": "stick",
    "slice": "slice",
    "slices": "slice",
    "piece": "piece",
    "pieces": "piece",
    "bunch": "bunch",
    "bunches": "bunch",
}

_MASS_UNITS = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mg": 0.001,
    "milligram": 0.001,
    "milligrams": 0.001,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}

def _clean_unit(unit_part: str) -> Optional[str]:
    unit_part = unit_part.strip()
    if not unit_part:
        return None
    tokens = [t.strip(".,") for t in unit_part.split()]
    if len(tokens) >= 2:
        first_two = " ".join(tokens[:2])
        if first_two in {"fl oz", "fluid ounce", "fluid ounces"}:
            return first_two
        last = tokens[-1]
        if last in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[last]
    return tokens[0]


def _normalize_fraction_text(text: str) -> str:
    for symbol, replacement in _UNICODE_FRACTIONS.items():
        text = text.replace(symbol, replacement)
    return text


def _parse_word_quantity(text: str) -> Tuple[Optional[float], str, bool]:
    tokens = text.split()
    if not tokens:
        return None, text, False
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""
    if first == "half" and second in {"a", "an"}:
        return 0.5, " ".join(tokens[2:]), True
    if first in {"a", "an"} and second == "half":
        return 0.5, " ".join(tokens[2:]), True
    if first in _WORD_QUANTITIES:
        return _WORD_QUANTITIES[first], " ".join(tokens[1:]), True
    return None, text, False


def _split_measurement(measurement: Any) -> Tuple[Optional[str], Optional[str], bool]:
    if measurement is None:
        return None, None, False
    if isinstance(measurement, (int, float)):
        return str(measurement), None, False
    text = _normalize_fraction_text(str(measurement).strip().lower())
    if not text:
        return None, None, False

    # Handle quantity ranges like "1/4 to 1/2 teaspoon ...".
    range_match = _RANGE_MEASUREMENT_RE.match(text)
    if range_match:
        q1 = _parse_quantity_value(range_match.group("q1"))
        q2 = _parse_quantity_value(range_match.group("q2"))
        if q1 is not None and q2 is not None:
            qty = str((q1 + q2) / 2.0)
            unit = _clean_unit(range_match.group("rest") or "")
            return qty, unit, False

    if not re.search(r"\d", text):
        qty_word, remainder, inferred = _parse_word_quantity(text)
        if qty_word is None:
            return None, None, False
        unit = _clean_unit(remainder or "")
        return str(qty_word), unit, inferred

    match = _MEASUREMENT_RE.match(text)
    if not match:
        return None, None, False
    qty = match.group("qty").strip()
    unit = _clean_unit(match.group("unit") or "")
    return qty, unit, False


def _infer_unit_from_name(name: str) -> Optional[str]:
    tokens = re.split(r"[\s,-]+", str(name).strip().lower())
    for token in tokens:
        if token in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[token]
    if len(tokens) == 1 and tokens[0]:
        return tokens[0]
    return None


def _parse_quantity_value(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = _normalize_fraction_text(str(value)).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if " " in text:
        whole, frac = text.split(" ", 1)
        try:
            return float(whole) + _parse_quantity_value(frac)
        except (TypeError, ValueError):
            return None
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            return float(num) / float(den)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


@tool
def ingredient_weight_tool_usda(
    ingredient_names: Any,
    measurements: Any,
    return_details: bool = False,
) -> list[float] | dict:
    """
    Calculates estimated weights using USDA portion data when possible.
    """
    names_list = [str(v) for v in _as_list(ingredient_names)]
    measures_list = _as_list(measurements)

    weights: list[float] = []
    details: list[dict] = []
    for idx, name in enumerate(names_list):
        measurement = measures_list[idx] if idx < len(measures_list) else None
        qty, unit, qty_inferred = _split_measurement(measurement)
        unit_inferred = False
        if qty is not None and unit is None:
            inferred_unit = _infer_unit_from_name(name)
            if inferred_unit:
                unit = inferred_unit
                unit_inferred = True
        link = canonical_name_to_usda(name)
        usda_id = link.get("usda_id") if link else None
        error = None
        portion_match = None
        match_type = None

        if not usda_id or qty is None or unit is None:
            if not usda_id:
                error = "missing_usda_id"
            elif qty is None:
                error = "missing_quantity"
            else:
                error = "missing_unit"
            weights.append(0.0)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": unit_inferred,
                "usda_id": usda_id,
                "portion_match": None,
                "match_type": None,
                "weight_grams": None,
                "error": error,
            })
            continue

        try:
            unit_norm = unit.strip().lower()
            if unit_norm in _MASS_UNITS:
                qty_value = _parse_quantity_value(qty)
                if qty_value is None:
                    raise ValueError("Invalid quantity value")
                grams = qty_value * _MASS_UNITS[unit_norm]
                match_type = "direct_mass"
            else:
                portion_match = match_portion(usda_id, unit, name=name)
                if portion_match:
                    match_type = "direct"
                else:
                    raise ValueError("No direct portion match")

                grams = weight_from_ingredient(
                    {"name": name, "usda_id": usda_id, "quantity": qty, "unit": unit}
                )
        except ValueError:
            grams = None
            error = "no_weight_match"

        weights.append(float(grams) if grams is not None else 0.0)
        details.append({
            "name": name,
            "measurement_raw": measurement,
            "parsed_quantity": qty,
            "parsed_unit": unit,
            "quantity_inferred": qty_inferred,
            "unit_inferred": unit_inferred,
            "usda_id": usda_id,
            "portion_match": portion_match,
            "match_type": match_type,
            "weight_grams": float(grams) if grams is not None else None,
            "error": error,
        })
    if return_details:
        return {"weights": weights, "details": details}
    return weights

    
def Ingredient_Weight_Node(state: RecipeState) -> RecipeState:
    debug = bool(state.debug)

    names = state.ingredient_names or []
    measurements = state.measurements or []
    result = ingredient_weight_tool_usda.invoke({
        "ingredient_names": names,
        "measurements": measurements,
        "return_details": True,
    })
    if isinstance(result, dict):
        state.weights = result.get("weights", [])
    else:
        state.weights = result

    trace = dict(state.pipeline_trace or {})
    if isinstance(result, dict):
        weight_details = result.get("details", [])
        trace["weight_calculation"] = {
            "weights": result.get("weights", []),
            "details": weight_details,
            "matched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is not None
            ),
            "unmatched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is None
            ),
        }
    else:
        trace["weight_calculation"] = {"weights": result}
    state.pipeline_trace = trace

    if debug:
        print("\n[Ingredient_Weight_Node] Used USDA weights to estimate grams.")
        print("[Ingredient_Weight_Node] Updated State Keys:", list(state.model_dump().keys()))

    return state
