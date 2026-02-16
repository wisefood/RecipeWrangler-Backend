# Purpose: Nutrition score computation helpers.

from __future__ import annotations

from typing import Optional

from pyNutriScore import NutriScore

from .usda_nutrients_v1 import fruits_veg_legumes_percent

_NUTRI_SCORE = NutriScore()

_GRADE_TO_LABEL = {
    "A": "Nutriscore_A",
    "B": "Nutriscore_B",
    "C": "Nutriscore_C",
    "D": "Nutriscore_D",
    "E": "Nutriscore_E",
}

_GRADE_TO_COLOR = {
    "A": "dark green",
    "B": "green",
    "C": "yellow",
    "D": "orange",
    "E": "dark orange",
}


def _nutrient_value(nutrients: dict, name: str) -> Optional[float]:
    entry = nutrients.get(name)
    if not entry:
        return None
    try:
        return float(entry.get("value"))
    except (TypeError, ValueError):
        return None


def _total_weight_grams(ingredients: list[dict]) -> float:
    total = 0.0
    for item in ingredients:
        try:
            total += float(item.get("weight_grams", 0))
        except (TypeError, ValueError):
            continue
    return total


def _per_100g(value: float, total_weight_g: float) -> float:
    return (value / total_weight_g) * 100.0


def compute_nutri_score(
    total_nutrients: dict,
    ingredients: list[dict],
) -> dict:
    nutrients = total_nutrients.get("nutrients", {})
    total_weight_g = _total_weight_grams(ingredients)
    if not nutrients or total_weight_g == 0.0:
        return {"error": "missing data"}

    raw_data = {
        "energy": _nutrient_value(nutrients, "Energy"),
        "sugar": _nutrient_value(nutrients, "Sugars, total"),
        "saturated_fats": _nutrient_value(nutrients, "Fatty acids, total saturated"),
        "sodium": _nutrient_value(nutrients, "Sodium, Na"),
        "fibers": _nutrient_value(nutrients, "Fiber, total dietary"),
        "proteins": _nutrient_value(nutrients, "Protein"),
    }
    if None in raw_data.values():
        return {"error": "missing data"}

    nutrient_values = {
        "energy": _per_100g(raw_data["energy"], total_weight_g),
        "sugar": _per_100g(raw_data["sugar"], total_weight_g),
        "saturated_fats": _per_100g(raw_data["saturated_fats"], total_weight_g),
        "sodium": _per_100g(raw_data["sodium"], total_weight_g),
        "fibers": _per_100g(raw_data["fibers"], total_weight_g),
        "proteins": _per_100g(raw_data["proteins"], total_weight_g),
        "fruit_percentage": fruits_veg_legumes_percent(ingredients),
    }

    try:
        score = _NUTRI_SCORE.calculate(nutrient_values, "solid")
        grade_key = str(_NUTRI_SCORE.calculate_class(nutrient_values, "solid")).upper()
    except Exception:
        return {"error": "missing data"}

    grade = _GRADE_TO_LABEL.get(grade_key)
    color = _GRADE_TO_COLOR.get(grade_key)
    if grade is None or color is None:
        return {"error": "missing data"}

    return {"score": score, "nutri_score": grade, "color": color}
