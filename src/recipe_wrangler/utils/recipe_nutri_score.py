# Purpose: Compute recipe-level USDA nutrition + nutri score from Recipe1M.

import json
from functools import lru_cache
from pathlib import Path

from .nutri_score import compute_nutri_score
from .usda_nutrients_v1 import canonical_name_to_usda, total_nutrients_for_ingredients
from .weigh_calculation_usda_ import weight_from_ingredient


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RECIPES = REPO_ROOT / "data/processed/recipe1m/recipe1m-ex-limited.json"


@lru_cache(maxsize=1)
def _recipes_by_id(recipes_path: Path) -> dict[str, dict]:
    data = json.loads(recipes_path.read_text(encoding="utf-8"))
    return {str(row.get("id")): row for row in data if row.get("id")}


def recipe_nutrition_and_score(
    recipe_id: str,
    recipes_path: Path = DEFAULT_RECIPES,
) -> dict:
    recipe = _recipes_by_id(recipes_path).get(str(recipe_id))
    if not recipe:
        raise ValueError(f"Recipe not found: {recipe_id}")
    ingredients = []
    for ingredient in recipe.get("ingredients", []):
        item = dict(ingredient)
        link = canonical_name_to_usda(item.get("name", ""))
        if not link:
            item["weight_grams"] = None
            ingredients.append(item)
            continue
        item["usda_id"] = link.get("usda_id")
        try:
            item["weight_grams"] = weight_from_ingredient(item)
        except ValueError:
            item["weight_grams"] = None
        ingredients.append(item)
    totals = total_nutrients_for_ingredients(ingredients)
    score = compute_nutri_score(totals, ingredients)
    return {
        "id": recipe.get("id"),
        "title": recipe.get("title"),
        "total_nutrients": totals,
        "nutri_score": score,
    }
