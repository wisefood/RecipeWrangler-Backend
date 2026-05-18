# Purpose: Nutrition score computation helpers.

from __future__ import annotations

from typing import Any, Optional

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


def compute_nutri_score_with_breakdown(
    total_nutrients: dict,
    ingredients: list[dict],
    food_type: str = "solid",
) -> dict:
    """Like ``compute_nutri_score`` but also returns the point-level breakdown.

    Returns ``{"error": ...}`` on missing data, otherwise
    ``{"score", "nutri_score", "color", "breakdown": {...}}``.
    """
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
        breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, food_type)
    except Exception:
        return {"error": "missing data"}
    breakdown["inputs"] = {"total_weight_g": total_weight_g}
    return {
        "score": breakdown["score"],
        "nutri_score": breakdown["nutri_score"],
        "color": breakdown["color"],
        "breakdown": breakdown,
    }


def compute_nutri_score_breakdown_from_values(
    nutrient_values: dict[str, float],
    food_type: str = "solid",
) -> dict[str, Any]:
    """Compute Nutri-Score with point-level breakdown from already-normalized per-100g values."""

    food_key = "beverage" if str(food_type).strip().lower() == "beverage" else "solid"

    score_table = _NUTRI_SCORE.score_table
    energy_points = _NUTRI_SCORE.nutrient_score(
        score_table["energy"][food_key], float(nutrient_values["energy"])
    )
    sugar_points = _NUTRI_SCORE.nutrient_score(
        score_table["sugar"][food_key], float(nutrient_values["sugar"])
    )
    sat_fat_points = _NUTRI_SCORE.nutrient_score(
        score_table["saturated_fats"][food_key], float(nutrient_values["saturated_fats"])
    )
    sodium_points = _NUTRI_SCORE.nutrient_score(
        score_table["sodium"][food_key], float(nutrient_values["sodium"])
    )

    fiber_points = _NUTRI_SCORE.nutrient_score(
        score_table["fibers"][food_key], float(nutrient_values["fibers"])
    )
    protein_points = _NUTRI_SCORE.nutrient_score(
        score_table["proteins"][food_key], float(nutrient_values["proteins"])
    )
    fruit_points = _NUTRI_SCORE.nutrient_score(
        score_table["fruit_percentage"][food_key], float(nutrient_values["fruit_percentage"])
    )

    negative_total = energy_points + sugar_points + sat_fat_points + sodium_points
    positive_raw_total = fiber_points + protein_points + fruit_points

    # Official rule: when negative >= 11 and fruit < 5, protein points are not subtracted.
    protein_excluded = bool(negative_total >= 11 and fruit_points < 5)
    positive_applied_total = (
        fiber_points + fruit_points if protein_excluded else positive_raw_total
    )
    final_score = negative_total - positive_applied_total

    grade_key = str(_NUTRI_SCORE.calculate_class(nutrient_values, food_key)).upper()
    grade = _GRADE_TO_LABEL.get(grade_key, "Nutriscore_C")
    color = _GRADE_TO_COLOR.get(grade_key, "yellow")

    return {
        "food_type": food_key,
        "score": final_score,
        "nutri_score": grade,
        "color": color,
        "negative_points": {
            "total": negative_total,
            "max": 40,
            "items": {
                "energy": {
                    "points": energy_points,
                    "max": 10,
                    "value_per_100g": float(nutrient_values["energy"]),
                    "unit": "kJ",
                },
                "sugar": {
                    "points": sugar_points,
                    "max": 10,
                    "value_per_100g": float(nutrient_values["sugar"]),
                    "unit": "g",
                },
                "saturated_fats": {
                    "points": sat_fat_points,
                    "max": 10,
                    "value_per_100g": float(nutrient_values["saturated_fats"]),
                    "unit": "g",
                },
                "sodium": {
                    "points": sodium_points,
                    "max": 10,
                    "value_per_100g": float(nutrient_values["sodium"]),
                    "unit": "mg",
                },
            },
        },
        "positive_points": {
            "total": positive_raw_total,
            "applied_total": positive_applied_total,
            "max": 15,
            "protein_excluded_by_rule": protein_excluded,
            "items": {
                "fiber": {
                    "points": fiber_points,
                    "max": 5,
                    "value_per_100g": float(nutrient_values["fibers"]),
                    "unit": "g",
                },
                "protein": {
                    "points": protein_points,
                    "max": 5,
                    "value_per_100g": float(nutrient_values["proteins"]),
                    "unit": "g",
                    "applied": not protein_excluded,
                },
                "fruit_percentage": {
                    "points": fruit_points,
                    "max": 5,
                    "value_per_100g": float(nutrient_values["fruit_percentage"]),
                    "unit": "%",
                },
            },
        },
    }
