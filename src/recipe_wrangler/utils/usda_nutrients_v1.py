# Purpose: Canonical->USDA link lookup and nutrient cache helpers.

import json
import os
from functools import lru_cache
import re
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LINKS = Path(
    os.getenv(
        "USDA_LINKS_PATH",
        str(REPO_ROOT / "data/mappings/recipe1m-usda-links-canonical.json"),
    )
)
DEFAULT_NUTRIENTS = REPO_ROOT / "data/processed/usda/usda-nutrients-v1.json"


@lru_cache(maxsize=1)
def _canonical_links(links_path: str) -> dict[str, dict]:
    data = json.loads(Path(links_path).read_text(encoding="utf-8"))
    return {
        str(row["canonical_id"]): row
        for row in data
        if row.get("canonical_id")
    }


@lru_cache(maxsize=1)
def _usda_nutrients(nutrients_path: str) -> dict[str, dict]:
    data = json.loads(Path(nutrients_path).read_text(encoding="utf-8"))
    return {str(row["usda_id"]): row for row in data if row.get("usda_id")}


def canonical_to_usda(
    canonical_id: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    return _canonical_links(str(links_path)).get(str(canonical_id))


def canonical_name_to_usda(
    canonical_name: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    canonical_lower = _normalize_canonical_name(str(canonical_name))
    for row in _canonical_links(str(links_path)).values():
        if _normalize_canonical_name(str(row.get("canonical", ""))) == canonical_lower:
            return row
    return None


def _normalize_canonical_name(name: str) -> str:
    cleaned = str(name).strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    tokens = cleaned.split()
    drop_tokens = {
        "fresh",
        "ground",
        "minced",
        "chopped",
        "large",
        "small",
        "medium",
        "to",
        "taste",
    }
    countable_suffixes = {
        "cloves": "clove",
        "clove": "clove",
        "sprigs": "sprig",
        "sprig": "sprig",
        "leaves": "leaf",
        "leaf": "leaf",
        "stalks": "stalk",
        "stalk": "stalk",
        "sticks": "stick",
        "stick": "stick",
        "slices": "slice",
        "slice": "slice",
        "pieces": "piece",
        "piece": "piece",
        "bunches": "bunch",
        "bunch": "bunch",
    }
    normalized = []
    for token in tokens:
        if token in drop_tokens:
            continue
        normalized.append(countable_suffixes.get(token, token))
    return " ".join(normalized).strip()


def usda_id_to_link(
    usda_id: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    usda_id_str = str(usda_id)
    for row in _canonical_links(str(links_path)).values():
        if str(row.get("usda_id")) == usda_id_str:
            return row
    return None


def nutrients_for_usda_id(
    usda_id: str,
    nutrients_path: Path = DEFAULT_NUTRIENTS,
) -> Optional[dict]:
    return _usda_nutrients(str(nutrients_path)).get(str(usda_id))


def total_nutrients_for_ingredients(
    ingredients: list[dict],
    nutrients_path: Path = DEFAULT_NUTRIENTS,
) -> dict:
    totals: dict[str, dict] = {}
    for ingredient in ingredients:
        usda_id = ingredient.get("usda_id")
        weight_grams = ingredient.get("weight_grams")
        if not usda_id or weight_grams is None:
            continue
        try:
            factor = float(weight_grams) / 100.0
        except (TypeError, ValueError):
            continue
        entry = nutrients_for_usda_id(str(usda_id), nutrients_path=nutrients_path)
        if not entry:
            continue
        for name, info in entry.get("nutrients", {}).items():
            try:
                value = float(info.get("value"))
            except (TypeError, ValueError):
                continue
            unit = info.get("unit")
            nutrient_id = info.get("nutrient_id")
            totals.setdefault(
                name,
                {"value": 0.0, "unit": unit, "nutrient_id": nutrient_id},
            )
            if totals[name]["unit"] != unit:
                continue
            totals[name]["value"] += value * factor
    return {"nutrients": totals}


def fruits_veg_legumes_percent(
    ingredients: list[dict],
    links_path: Path = DEFAULT_LINKS,
) -> float:
    target_groups = {
        "Fruits and Fruit Juices",
        "Vegetables and Vegetable Products",
        "Legumes and Legume Products",
        "Nut and Seed Products",
    }
    oil_names = {
        "olive oil",
        "olive oils",
        "rapeseed oil",
        "canola oil",
        "walnut oil",
    }
    total_weight = 0.0
    target_weight = 0.0
    for ingredient in ingredients:
        weight = ingredient.get("weight_grams")
        usda_id = ingredient.get("usda_id")
        if weight is None or not usda_id:
            continue
        try:
            weight_f = float(weight)
        except (TypeError, ValueError):
            continue
        total_weight += weight_f
        link = usda_id_to_link(usda_id, links_path=links_path)
        name_lower = str(ingredient.get("name", "")).strip().lower()
        if name_lower in oil_names:
            target_weight += weight_f
            continue
        if link and link.get("food_group") in target_groups:
            target_weight += weight_f
    if total_weight == 0.0:
        return 0.0
    return (target_weight / total_weight) * 100.0


def nutrients_for_canonical_id(
    canonical_id: str,
    links_path: Path = DEFAULT_LINKS,
    nutrients_path: Path = DEFAULT_NUTRIENTS,
) -> Optional[dict]:
    link = canonical_to_usda(str(canonical_id), links_path)
    usda_id = link.get("usda_id") if link else None
    if not usda_id:
        return None
    return _usda_nutrients(str(nutrients_path)).get(str(usda_id))
