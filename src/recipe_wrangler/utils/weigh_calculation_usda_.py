# Purpose: USDA weight/portion calculation helpers.

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WEIGHTS = REPO_ROOT / "data/processed/usda/usda-weights-v1.json"

VOLUME_TO_CUPS = {
    "tsp": 1.0 / 48.0,
    "teaspoon": 1.0 / 48.0,
    "tbsp": 1.0 / 16.0,
    "tablespoon": 1.0 / 16.0,
    "oz": 1.0 / 8.0,
    "ounce": 1.0 / 8.0,
    "fluid ounce": 1.0 / 8.0,
    "cup": 1.0,
    "pint": 2.0,
    "quart": 4.0,
    "gallon": 16.0,
    "ml": 1.0 / 236.5882365,
    "milliliter": 1.0 / 236.5882365,
    "l": 4.22675284,
    "liter": 4.22675284,
}


@lru_cache(maxsize=1)
def _weights_by_food(weights_path: str) -> dict[str, list[dict]]:
    data = json.loads(Path(weights_path).read_text(encoding="utf-8"))
    return {str(row["usda_id"]): row.get("portions", []) for row in data if row.get("usda_id")}


def _norm_unit(unit: str) -> str:
    unit = unit.strip().lower()
    aliases = {
        "tb": "tablespoon",
        "tbl": "tablespoon",
        "tbsp": "tablespoon",
        "tbsps": "tablespoon",
        "tablespoons": "tablespoon",
        "t": "teaspoon",
        "tsp": "teaspoon",
        "teaspoons": "teaspoon",
        "ounces": "ounce",
        "ozs": "ounce",
        "oz": "ounce",
        "fl oz": "fluid ounce",
        "fluid ounces": "fluid ounce",
        "cups": "cup",
        "c": "cup",
        "lbs": "pound",
        "lb": "pound",
        "pounds": "pound",
    }
    return aliases.get(unit, unit)


def _portion_unit(portion_desc: str) -> str:
    cleaned = portion_desc.strip().lower()
    cleaned = cleaned.split("(")[0].strip()
    cleaned = re.sub(r"^[0-9./\\s]+", "", cleaned).strip()
    return _norm_unit(cleaned)

def _parse_quantity(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    range_match = re.match(r"^([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)$", text)
    if range_match:
        start = float(range_match.group(1))
        end = float(range_match.group(2))
        return (start + end) / 2.0
    if " " in text:
        whole, frac = text.split(" ", 1)
        return float(whole) + _parse_quantity(frac)
    if "/" in text:
        num, den = text.split("/", 1)
        den_f = float(den)
        if den_f == 0:
            raise ValueError("Invalid quantity value")
        return float(num) / den_f
    return float(text)


def grams_for_food_id(
    usda_id: str,
    unit: str,
    value: float,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[float]:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id), [])
    if not portions:
        print(f"WARNING: no weights found for usda_id={usda_id}")
        return None

    unit_norm = _norm_unit(unit)
    matches = [p for p in portions if _portion_unit(str(p.get("portion_desc", ""))) == unit_norm]
    if not matches:
        print(f"WARNING: no unit match for usda_id={usda_id} unit={unit}")
        return None
    first = matches[0]
    try:
        grams_per_unit_f = float(first.get("grams_per_unit"))
    except (TypeError, ValueError):
        return None
    try:
        qty = _parse_quantity(value)
    except (TypeError, ValueError):
        return None
    return grams_per_unit_f * qty


def get_portions(
    usda_id: str,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> dict:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id))
    if not portions:
        raise ValueError(f"No portions found for usda_id={usda_id}")
    return {"usda_id": str(usda_id), "portions": portions}


def match_portion(
    usda_id: str,
    unit: str,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[dict]:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id), [])
    if not portions:
        return None
    unit_norm = _norm_unit(str(unit))
    for portion in portions:
        if _portion_unit(str(portion.get("portion_desc", ""))) == unit_norm:
            return portion
    return None


def fallback_portion(
    usda_id: str,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[dict]:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id), [])
    if not portions:
        return None
    for portion in portions:
        unit_candidate = _portion_unit(str(portion.get("portion_desc", "")))
        if unit_candidate in VOLUME_TO_CUPS:
            return {"portion": portion, "unit": unit_candidate}
    return None


def weight_from_ingredient(
    ingredient: dict,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> float:
    usda_id = ingredient.get("usda_id")
    unit = ingredient.get("unit")
    quantity = ingredient.get("quantity")
    name = ingredient.get("name")
    if not usda_id or unit is None or quantity is None:
        raise ValueError("Ingredient must include usda_id, unit, and quantity")

    # If the unit is already grams, return the provided quantity directly.
    unit_norm = _norm_unit(str(unit))
    if unit_norm in {"g", "gram", "grams"}:
        try:
            return _parse_quantity(quantity)
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity value")

    portions = _weights_by_food(str(weights_path)).get(str(usda_id))
    if not portions:
        raise ValueError(f"No portions found for usda_id={usda_id}")

    matches = [p for p in portions if _portion_unit(str(p.get("portion_desc", ""))) == unit_norm]
    try:
        qty = _parse_quantity(quantity)
    except (TypeError, ValueError):
        raise ValueError("Invalid quantity value")

    if matches:
        try:
            grams_per_unit = float(matches[0].get("grams_per_unit"))
        except (TypeError, ValueError):
            raise ValueError("Invalid grams_per_unit value")
        return qty * grams_per_unit

    fallback = None
    fallback_unit = None
    for portion in portions:
        unit_candidate = _portion_unit(str(portion.get("portion_desc", "")))
        if unit_candidate in VOLUME_TO_CUPS:
            fallback = portion
            fallback_unit = unit_candidate
            break
    if not fallback:
        raise ValueError(f"No unit match found for usda_id={usda_id} name={name}")
    try:
        grams_per_unit = float(fallback.get("grams_per_unit"))
    except (TypeError, ValueError):
        raise ValueError("Invalid grams_per_unit value")
    unit_to_cups = VOLUME_TO_CUPS.get(unit_norm)
    fallback_to_cups = VOLUME_TO_CUPS.get(fallback_unit)
    if unit_to_cups is None or fallback_to_cups is None:
        raise ValueError(f"No unit match found for usda_id={usda_id} name={name}")
    converted_units = qty * (unit_to_cups / fallback_to_cups)
    grams = converted_units * grams_per_unit
    return grams
