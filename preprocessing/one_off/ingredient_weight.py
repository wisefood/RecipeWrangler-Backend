import json
from pathlib import Path
from typing import Dict, List, Optional


def _normalize_unit(value: str) -> str:
    value = value.strip().lower()
    value = value.replace(".", "")
    value = value.replace("-", " ")
    if value.endswith("s") and len(value) > 2:
        value = value[:-1]
    return value


def _unit_tokens(unit: str) -> list[str]:
    unit = _normalize_unit(unit)
    tokens = [unit]
    synonyms = {
        "ounce": ["oz"],
        "oz": ["ounce"],
        "tablespoon": ["tbsp"],
        "tbsp": ["tablespoon"],
        "teaspoon": ["tsp"],
        "tsp": ["teaspoon"],
        "cup": ["cups"],
        "cups": ["cup"],
        "fl oz": ["floz", "fluid ounce", "fluid ounces"],
        "floz": ["fl oz", "fluid ounce", "fluid ounces"],
    }
    tokens.extend(synonyms.get(unit, []))
    return list(dict.fromkeys(tokens))


class IngredientWeightResolver:
    def __init__(self, weights_path: Path):
        self._weights_by_food: Dict[str, List[dict]] = {}
        data = json.loads(weights_path.read_text(encoding="utf-8"))
        for row in data:
            food_id = row.get("food_id")
            portion_desc = row.get("portion_desc")
            grams = row.get("grams")
            amount = row.get("amount")
            if not food_id or portion_desc is None or grams is None or amount in (None, 0):
                continue
            self._weights_by_food.setdefault(str(food_id), []).append(
                {
                    "portion_desc": _normalize_unit(str(portion_desc)),
                    "grams": float(grams),
                    "amount": float(amount),
                }
            )

    def resolve_grams(self, food_id: str, quantity: Optional[float], unit: Optional[str]) -> Optional[float]:
        if not food_id or quantity is None or unit is None:
            return None
        entries = self._weights_by_food.get(str(food_id))
        if not entries:
            return None

        unit_tokens = _unit_tokens(str(unit))
        matches = [
            e
            for e in entries
            if any(token in e["portion_desc"] for token in unit_tokens)
        ]
        if not matches:
            return None

        # Prefer the closest portion amount, then scale by quantity.
        matches.sort(key=lambda e: abs(e["amount"] - float(quantity)))
        best = matches[0]
        grams_per_unit = best["grams"] / best["amount"]
        return float(quantity) * grams_per_unit
