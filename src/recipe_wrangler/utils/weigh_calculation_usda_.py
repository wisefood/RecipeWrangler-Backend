# Purpose: USDA weight/portion calculation helpers.

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WEIGHTS = REPO_ROOT / "data/processed/usda/usda-weights-v2.json"


@lru_cache(maxsize=1)
def _weights_by_food(weights_path: str) -> dict[str, list[dict]]:
    data = json.loads(Path(weights_path).read_text(encoding="utf-8"))
    return {str(row["usda_id"]): row.get("portions", []) for row in data if row.get("usda_id")}

@lru_cache(maxsize=1)
def _weights_by_name(weights_path: str) -> dict[str, list[dict]]:
    data = json.loads(Path(weights_path).read_text(encoding="utf-8"))
    by_name: dict[str, list[dict]] = {}
    for row in data:
        name = row.get("food_name")
        if not name:
            continue
        key = str(name).strip().lower()
        if not key:
            continue
        by_name.setdefault(key, []).extend(row.get("portions", []))
    return by_name


@lru_cache(maxsize=1)
def _weight_rows(weights_path: str) -> list[dict]:
    return json.loads(Path(weights_path).read_text(encoding="utf-8"))


def _portions_for_food(
    usda_id: Optional[str],
    name: Optional[str],
    weights_path: str,
) -> list[dict]:
    portions = []
    if usda_id:
        portions = _weights_by_food(weights_path).get(str(usda_id), [])
    if not portions and name:
        portions = _weights_by_name(weights_path).get(str(name).strip().lower(), [])
    return portions


def find_weight_match_by_name(
    name: str,
    unit: str,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[dict]:
    key = str(name).strip().lower()
    if not key:
        return None

    unit_norm = _norm_unit(str(unit))
    if not unit_norm:
        return None

    rows = _weight_rows(str(weights_path))
    exact = []
    prefix = []
    contains = []
    for row in rows:
        food_name = str(row.get("food_name", "")).strip()
        if not food_name:
            continue
        food_key = food_name.lower()
        if food_key == key:
            exact.append(row)
        elif food_key.startswith(f"{key},") or food_key.startswith(f"{key} "):
            prefix.append(row)
        elif key in food_key:
            contains.append(row)

    for candidates in (exact, sorted(prefix, key=lambda r: len(str(r.get("food_name", "")))), sorted(contains, key=lambda r: len(str(r.get("food_name", ""))))):
        for row in candidates:
            food_name = str(row.get("food_name", "")).strip()
            for portion in row.get("portions", []):
                portion_unit = _portion_unit(str(portion.get("portion_desc", "")))
                if portion_unit == unit_norm:
                    return {
                        "food_name": food_name,
                        "usda_id": str(row.get("usda_id")) if row.get("usda_id") is not None else None,
                        "portion": portion,
                    }
    return None


def _norm_unit(unit: str) -> str:
    unit = unit.strip().lower().strip(".,")
    aliases = {
        "tb": "tablespoon",
        "tbl": "tablespoon",
        "tbsp": "tablespoon",
        "tbsp.": "tablespoon",
        "tbsps": "tablespoon",
        "tablespoons": "tablespoon",
        "t": "teaspoon",
        "tsp": "teaspoon",
        "tsp.": "teaspoon",
        "teaspoons": "teaspoon",
        "ounces": "ounce",
        "ozs": "ounce",
        "oz": "ounce",
        "oz.": "ounce",
        "fl oz": "fluid ounce",
        "fluid ounces": "fluid ounce",
        "cups": "cup",
        "c": "cup",
        "c.": "cup",
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

def _combine_measurement(quantity: object, unit: object) -> Optional[str]:
    qty = "" if quantity is None else str(quantity).strip()
    unit_s = "" if unit is None else str(unit).strip()
    combined = f"{qty} {unit_s}".strip()
    return combined or None

def _parse_measurement(measurement: object) -> tuple[Optional[str], Optional[str]]:
    if measurement is None:
        return None, None
    text = str(measurement).strip()
    if not text:
        return None, None
    match = re.match(r"^([0-9./\\s-]+)\\s*(.*)$", text)
    if not match:
        return None, None
    qty = match.group(1).strip() or None
    unit = match.group(2).strip() or None
    return qty, unit


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
    name: Optional[str] = None,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[dict]:
    portions = _portions_for_food(usda_id, name, str(weights_path))
    if not portions:
        return None
    unit_norm = _norm_unit(str(unit))
    for portion in portions:
        if _portion_unit(str(portion.get("portion_desc", ""))) == unit_norm:
            return portion
    return None


def weight_from_ingredient(
    ingredient: dict,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> float:
    usda_id = ingredient.get("usda_id")
    unit = ingredient.get("unit")
    quantity = ingredient.get("quantity")
    name = ingredient.get("name")
    measurement = ingredient.get("measurement")

    if not measurement:
        measurement = _combine_measurement(quantity, unit)
        if measurement:
            ingredient["measurement"] = measurement

    if (unit is None or str(unit).strip() == "" or quantity is None) and measurement:
        parsed_qty, parsed_unit = _parse_measurement(measurement)
        if quantity is None and parsed_qty is not None:
            quantity = parsed_qty
        if (unit is None or str(unit).strip() == "") and parsed_unit is not None:
            unit = parsed_unit

    if not usda_id or unit is None or quantity is None:
        raise ValueError("Ingredient must include usda_id, unit, and quantity")

    # If the unit is already grams, return the provided quantity directly.
    unit_norm = _norm_unit(str(unit))
    if unit_norm in {"g", "gram", "grams"}:
        try:
            return _parse_quantity(quantity)
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity value")

    portions = _portions_for_food(usda_id, name, str(weights_path))
    if not portions:
        raise ValueError(f"No portions found for usda_id={usda_id} name={name}")

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

    raise ValueError(f"No unit match found for usda_id={usda_id} name={name}")
