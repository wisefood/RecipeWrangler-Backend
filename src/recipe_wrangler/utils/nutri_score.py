# Purpose: Nutrition score computation helpers.

from __future__ import annotations

from typing import Optional

from .usda_nutrients_v1 import fruits_veg_legumes_percent

ENERGY_POINTS = [
    (335, 1), (670, 2), (1005, 3), (1340, 4), (1675, 5),
    (2010, 6), (2345, 7), (2680, 8), (3015, 9), (3350, 10),
    (3685, 11), (4020, 12), (4355, 13), (4690, 14), (5025, 15),
    (5360, 16), (5695, 17), (6030, 18), (6365, 19), (6700, 20),
]

SUGAR_POINTS = [
    (3.4, 1), (6.8, 2), (10.0, 3), (14.0, 4), (17.0, 5),
    (20.0, 6), (24.0, 7), (27.0, 8), (31.0, 9), (34.0, 10),
    (37.0, 11), (41.0, 12), (44.0, 13), (48.0, 14), (51.0, 15),
]

SATURATES_POINTS = [
    (1.0, 1), (2.0, 2), (3.0, 3), (4.0, 4), (5.0, 5),
    (6.0, 6), (7.0, 7), (8.0, 8), (9.0, 9), (10.0, 10),
]

SODIUM_POINTS = [
    (80, 1), (160, 2), (240, 3), (320, 4), (400, 5), (480, 6),
    (560, 7), (640, 8), (720, 9), (800, 10), (880, 11), (960, 12),
    (1040, 13), (1120, 14), (1200, 15), (1280, 16), (1360, 17),
    (1440, 18), (1520, 19), (1600, 20),
]

FRUIT_VEG_POINTS = [
    (40.0, 1), (60.0, 2), (80.0, 5),
]

FIBRE_POINTS = [
    (3.0, 1), (4.1, 2), (5.2, 3), (6.3, 4), (7.4, 5),
]

PROTEIN_POINTS = [
    (2.4, 1), (4.8, 2), (7.2, 3), (9.6, 4), (12.0, 5), (14.0, 6), (17.0, 7),
]

def _points_for(value: float, thresholds: list[tuple[float, int]], max_points: int) -> int:
    points = 0
    for limit, next_points in thresholds:
        if value > limit:
            points = next_points
        else:
            break
    return min(points, max_points)

def _nutrient_value(nutrients: dict, name: str) -> Optional[float]:
    entry = nutrients.get(name)
    if not entry: return None
    try: return float(entry.get("value"))
    except (TypeError, ValueError): return None

def _total_weight_grams(ingredients: list[dict]) -> float:
    total = 0.0
    for i in ingredients:
        try: total += float(i.get("weight_grams", 0))
        except (TypeError, ValueError): continue
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

    # Extract raw values
    raw_data = {
        "energy": _nutrient_value(nutrients, "Energy"),
        "sugar": _nutrient_value(nutrients, "Sugars, total"),
        "saturates": _nutrient_value(nutrients, "Fatty acids, total saturated"),
        "sodium": _nutrient_value(nutrients, "Sodium, Na"),
        "fibre": _nutrient_value(nutrients, "Fiber, total dietary"),
        "protein": _nutrient_value(nutrients, "Protein"),
    }
    
    if None in raw_data.values():
        return {"error": "missing data"}

    # Normalize to 100g
    energy = _per_100g(raw_data["energy"], total_weight_g)
    sugar = _per_100g(raw_data["sugar"], total_weight_g)
    saturates = _per_100g(raw_data["saturates"], total_weight_g)
    sodium = _per_100g(raw_data["sodium"], total_weight_g)
    fibre = _per_100g(raw_data["fibre"], total_weight_g)
    protein = _per_100g(raw_data["protein"], total_weight_g)
    
    fruit_veg_pct = fruits_veg_legumes_percent(ingredients)

    # 1. Calculate Points
    energy_pts = _points_for(energy, ENERGY_POINTS, 20)
    sugar_pts = _points_for(sugar, SUGAR_POINTS, 15)
    saturates_pts = _points_for(saturates, SATURATES_POINTS, 10)
    salt_pts = _points_for(sodium, SODIUM_POINTS, 20)
    
    fruit_veg_pts = _points_for(fruit_veg_pct, FRUIT_VEG_POINTS, 5)
    fibre_pts = _points_for(fibre, FIBRE_POINTS, 5)
    protein_pts = _points_for(protein, PROTEIN_POINTS, 7)

    negative_points = energy_pts + sugar_pts + saturates_pts + salt_pts
    positive_points = fruit_veg_pts + fibre_pts + protein_pts

    # 2. Apply Protein Gatekeeper (Standard Logic)
    if negative_points < 11 or fruit_veg_pts >= 5:
        score = negative_points - positive_points
    else:
        # Protein is ignored if product is too high in "negative" nutrients
        score = negative_points - (fruit_veg_pts + fibre_pts)

    # 3. Final Grade Thresholds
    if score <= 0:
        grade, color = "Nutriscore_A", "dark green"
    elif score <= 2:
        grade, color = "Nutriscore_B", "green"
    elif score <= 10:
        grade, color = "Nutriscore_C", "yellow"
    elif score <= 18:
        grade, color = "Nutriscore_D", "orange"
    else:
        grade, color = "Nutriscore_E", "dark orange"

    return {"score": score, "nutri_score": grade, "color": color}
