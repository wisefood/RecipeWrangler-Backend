"""Canonical food-group to Eurostat PLI-category mappings."""

from __future__ import annotations

import json
from pathlib import Path


_MAPPING_PATH = Path(__file__).with_name("product_mapping_overrides.json")
FOOD_GROUP_TO_PLI = json.loads(_MAPPING_PATH.read_text(encoding="utf-8"))["pli_categories"]


def pli_category_for(food_group: str) -> str:
    try:
        return FOOD_GROUP_TO_PLI[food_group]
    except KeyError as exc:
        raise ValueError(f"No PLI category for food group {food_group!r}") from exc
