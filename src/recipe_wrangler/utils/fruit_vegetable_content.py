"""Estimate Nutri-Score fruit/vegetable/legume/nut content without USDA data."""

from __future__ import annotations

import re
from typing import Any

from recipe_wrangler.catalog.vocabularies import food_groups_from_foodon

_TARGET_GROUPS = {"fruit", "vegetables", "legumes", "nuts_and_seeds"}
_APPROVED_OILS = {
    "olive oil",
    "olive oils",
    "rapeseed oil",
    "canola oil",
    "walnut oil",
}
_TARGET_NAME_RE = re.compile(
    r"\b(?:"
    r"apple|apricot|avocado|banana|berr(?:y|ies)|cherr(?:y|ies)|citrus|"
    r"clementine|cranberr(?:y|ies)|currant|date|fig|grape|grapefruit|guava|"
    r"kiwi|lemon|lime|lychee|mandarin|mango|melon|nectarine|orange|papaya|"
    r"passion\s*fruit|peach|pear|persimmon|pineapple|plum|pomegranate|prune|"
    r"raisin|raspberr(?:y|ies)|rhubarb|strawberr(?:y|ies)|sultana|tangerine|"
    r"watermelon|"
    r"artichoke|asparagus|aubergine|beet(?:root)?|bok\s*choy|broccoli|"
    r"brussels?\s*sprout|cabbage|carrot|cauliflower|celery|chard|chayote|"
    r"collard|corn|courgette|cucumber|daikon|eggplant|endive|fennel|garlic|"
    r"green\s*bean|jicama|kale|kohlrabi|leek|lettuce|mushroom|okra|onion|"
    r"pak\s*choi|parsnip|peas?|pepper|plantain|potato|pumpkin|radicchio|"
    r"radish|rocket|romaine|rutabaga|scallion|shallot|spinach|squash|swede|"
    r"sweet\s*potato|sweetcorn|tomato|turnip|watercress|yam|zucchini|"
    r"bean|beans|black[- ]?eyed\s*pea|chickpea|chickpeas|edamame|garbanzo|"
    r"lentil|lentils|split\s*pea|"
    r"almond|brazil\s*nut|cashew|chia|flax(?:seed)?|hazelnut|macadamia|"
    r"peanut|pecan|pine\s*nut|pistachio|pumpkin\s*seed|sesame|sunflower\s*seed|"
    r"tahini|walnut"
    r")\b",
    re.IGNORECASE,
)


def _explicit_food_groups(ingredient: dict[str, Any]) -> set[str]:
    groups = ingredient.get("food_groups") or ingredient.get("food_group") or []
    if isinstance(groups, str):
        groups = [groups]
    result = {str(group).strip().lower() for group in groups if str(group).strip()}

    ancestors = ingredient.get("ingredient_class_ancestors") or ingredient.get(
        "foodon_ancestor_ids"
    )
    if isinstance(ancestors, (list, tuple, set)):
        result.update(food_groups_from_foodon(ancestors))
    return result


def is_fruit_vegetable_legume_or_nut(ingredient: dict[str, Any]) -> bool:
    """Return whether an ingredient contributes to the Nutri-Score FVNL share."""
    groups = _explicit_food_groups(ingredient)
    if groups:
        return bool(groups & _TARGET_GROUPS)

    name = str(ingredient.get("name") or ingredient.get("ingredient") or "").strip().lower()
    if name in _APPROVED_OILS:
        return True
    return bool(_TARGET_NAME_RE.search(name))


def fruits_veg_legumes_percent(ingredients: list[dict[str, Any]]) -> float:
    """Return the recipe-weight percentage from fruit, veg, legumes, and nuts."""
    total_weight = 0.0
    target_weight = 0.0
    for ingredient in ingredients:
        weight = ingredient.get("weight_grams", ingredient.get("weight_g"))
        try:
            weight_f = float(weight)
        except (TypeError, ValueError):
            continue
        if weight_f <= 0:
            continue
        total_weight += weight_f
        if is_fruit_vegetable_legume_or_nut(ingredient):
            target_weight += weight_f
    if total_weight <= 0.0:
        return 0.0
    return (target_weight / total_weight) * 100.0
