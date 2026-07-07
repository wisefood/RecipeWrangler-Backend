"""Chroma repository adapter for nutrition/sustainability candidate lookup."""

from __future__ import annotations

from recipe_wrangler.utils.query_chromadb import (
    query_nutritional_db_eu,
    query_nutritional_db_hungarian,
    query_nutritional_db_irish,
    query_nutritional_db_usda,
    query_sustainability_db,
)


def query_irish_nutrition_candidates(name: str) -> list[dict]:
    return query_nutritional_db_irish(name) or []


def query_usda_nutrition_candidates(name: str) -> list[dict]:
    return query_nutritional_db_usda(name) or []


def query_hungarian_nutrition_candidates(name: str) -> list[dict]:
    return query_nutritional_db_hungarian(name) or []


def query_eu_nutrition_candidates(name: str) -> list[dict]:
    return query_nutritional_db_eu(name) or []


def query_sustainability_candidates(name: str) -> list[dict]:
    return query_sustainability_db(name) or []
