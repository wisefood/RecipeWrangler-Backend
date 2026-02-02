from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .ingredient_weight import IngredientWeightResolver
from .usda_nutrients import USDANutrientLookup


def load_canonical_usda_links(links_path: Path) -> Dict[str, str]:
    data = json.loads(links_path.read_text(encoding="utf-8"))
    by_id: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    for row in data:
        canonical_id = row.get("canonical_id")
        canonical_name = row.get("canonical")
        usda_id = row.get("usda_id")
        if not usda_id:
            continue
        if canonical_id:
            by_id[str(canonical_id)] = str(usda_id)
        if canonical_name:
            by_name[str(canonical_name)] = str(usda_id)
    return {"by_id": by_id, "by_name": by_name}


def _extract_canonical_key(ing: dict) -> Optional[str]:
    for key in ("canonical_id", "canonical", "canonical_name", "name"):
        value = ing.get(key)
        if value:
            return str(value)
    return None


def _extract_quantity_unit(ing: dict) -> tuple[Optional[float], Optional[str]]:
    qty = ing.get("quantity")
    unit = ing.get("unit")
    try:
        qty = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    unit = str(unit) if unit else None
    return qty, unit


def compute_recipe_nutrients(
    recipe: dict,
    usda_links: Dict[str, Dict[str, str]],
    weight_resolver: IngredientWeightResolver,
    nutrient_lookup: USDANutrientLookup,
) -> Optional[dict]:
    recipe_id = recipe.get("id")
    if not recipe_id:
        return None

    totals: Dict[str, dict] = {}
    skipped = 0

    for ing in recipe.get("ingredients", []):
        if not isinstance(ing, dict):
            continue
        canonical_key = _extract_canonical_key(ing)
        if not canonical_key:
            skipped += 1
            continue

        usda_id = usda_links["by_id"].get(canonical_key) or usda_links["by_name"].get(canonical_key)
        if not usda_id:
            skipped += 1
            continue

        qty, unit = _extract_quantity_unit(ing)
        grams = weight_resolver.resolve_grams(usda_id, qty, unit)
        if grams is None:
            skipped += 1
            continue

        nutrients = nutrient_lookup.nutrients_for_food(usda_id)
        for row in nutrient_lookup.scale_nutrients(nutrients, grams):
            nutrient_id = row["nutrient_id"]
            entry = totals.get(nutrient_id)
            if entry is None:
                totals[nutrient_id] = {
                    "nutrient_id": nutrient_id,
                    "nutrient_description": row.get("nutrient_description"),
                    "unit": row["unit"],
                    "value": row["value"],
                }
            else:
                entry["value"] += row["value"]

    return {
        "recipe_id": recipe_id,
        "nutrients": list(totals.values()),
        "ingredients_skipped": skipped,
    }
