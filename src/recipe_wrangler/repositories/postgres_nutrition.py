"""Postgres nutrition repository adapter."""

from __future__ import annotations

from typing import Optional

from recipe_wrangler.utils.nutrition_postgres import (
    fetch_ingredient_nutrition_by_canonical_id_hungarian,
    fetch_ingredient_nutrition_by_canonical_id_irish,
    fetch_ingredient_nutrition_by_usda_id,
    fetch_recipe_nutrition_by_id,
    fetch_recipe_profiling_trace_by_id,
    upsert_recipe_profiling_trace,
)


def get_recipe_nutrition(recipe_id: str) -> Optional[dict]:
    return fetch_recipe_nutrition_by_id(recipe_id)


def get_recipe_profile_trace(recipe_id: str) -> Optional[dict]:
    return fetch_recipe_profiling_trace_by_id(recipe_id)


def save_recipe_profile_trace(record: dict) -> None:
    upsert_recipe_profiling_trace(record)


def get_usda_ingredient_nutrition(usda_id: str) -> Optional[dict]:
    return fetch_ingredient_nutrition_by_usda_id(usda_id)


def get_irish_ingredient_nutrition(canonical_food_id: str) -> Optional[dict]:
    return fetch_ingredient_nutrition_by_canonical_id_irish(canonical_food_id)


def get_hungarian_ingredient_nutrition(canonical_food_id: str) -> Optional[dict]:
    return fetch_ingredient_nutrition_by_canonical_id_hungarian(canonical_food_id)
