import json
from pathlib import Path
from typing import Dict, List


class USDANutrientLookup:
    def __init__(self, nutrients_path: Path):
        self._by_food: Dict[str, List[dict]] = {}
        data = json.loads(nutrients_path.read_text(encoding="utf-8"))
        for row in data:
            usda_id = row.get("usda_id")
            nutrient_id = row.get("nutrient_id")
            value = row.get("nutrient_value")
            unit = row.get("unit")
            if not usda_id or nutrient_id is None or value is None or unit is None:
                continue
            self._by_food.setdefault(str(usda_id), []).append(
                {
                    "nutrient_id": str(nutrient_id),
                    "nutrient_description": row.get("nutrient_description"),
                    "unit": unit,
                    "value_per_100g": float(value),
                }
            )

    def nutrients_for_food(self, food_id: str) -> List[dict]:
        return list(self._by_food.get(str(food_id), []))

    @staticmethod
    def scale_nutrients(nutrients: List[dict], grams: float) -> List[dict]:
        factor = grams / 100.0
        scaled = []
        for row in nutrients:
            scaled.append(
                {
                    "nutrient_id": row["nutrient_id"],
                    "nutrient_description": row.get("nutrient_description"),
                    "unit": row["unit"],
                    "value": row["value_per_100g"] * factor,
                }
            )
        return scaled
