"""Deterministic recipe search using explicit filter parameters."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.utils.neo4j_utils import run_query


def _normalize_terms(items: list[str]) -> list[str]:
    cleaned = [item.strip().casefold() for item in items if str(item).strip()]
    # Preserve first occurrence order while de-duplicating.
    return list(dict.fromkeys(cleaned))


def build_param_search_cypher(filters: RecipeSearchFilters) -> tuple[str, dict[str, Any]]:
    """Build a parameterized Cypher query from explicit filters."""

    include_ingredients = _normalize_terms(filters.include_ingredients)
    exclude_ingredients = _normalize_terms(filters.exclude_ingredients)
    exclude_allergens = _normalize_terms(filters.exclude_allergens)
    diet_tags = _normalize_terms(filters.diet_tags)
    limit = max(1, min(int(filters.limit), 100))

    predicates: list[str] = []
    params: dict[str, Any] = {"limit": limit}

    if include_ingredients:
        predicates.append(
            "ALL(ing IN $include_ingredients WHERE EXISTS { "
            "MATCH (r)-[:HAS_INGREDIENT]->(i_inc:Ingredient) "
            "WHERE toLower(i_inc.name) CONTAINS ing "
            "})"
        )
        params["include_ingredients"] = include_ingredients

    if exclude_ingredients:
        predicates.append(
            "NOT EXISTS { "
            "MATCH (r)-[:HAS_INGREDIENT]->(i_exc:Ingredient) "
            "WHERE ANY(ing IN $exclude_ingredients WHERE toLower(i_exc.name) CONTAINS ing) "
            "}"
        )
        params["exclude_ingredients"] = exclude_ingredients

    if exclude_allergens:
        predicates.append(
            "NOT EXISTS { "
            "MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen) "
            "WHERE toLower(al.name) IN $exclude_allergens "
            "}"
        )
        params["exclude_allergens"] = exclude_allergens

    if diet_tags:
        predicates.append(
            "EXISTS { "
            "MATCH (r)-[:HAS_TAG]->(t:Tag) "
            "WHERE toLower(t.name) IN $diet_tags "
            "}"
        )
        params["diet_tags"] = diet_tags

    if filters.max_duration_minutes is not None:
        predicates.append(
            "coalesce(toInteger(r.duration), 999999) <= $max_duration_minutes"
        )
        params["max_duration_minutes"] = int(filters.max_duration_minutes)

    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    query = f"""
    MATCH (r:Recipe)
    {where_clause}
    RETURN
      r.recipe_id AS recipe_id,
      r.title AS title
    LIMIT $limit
    """
    return query, params


def search_recipes_by_params(filters: RecipeSearchFilters) -> list[dict[str, Any]]:
    """Execute deterministic parameter-based recipe search."""

    query, params = build_param_search_cypher(filters)
    rows = run_query(query, params)
    return [dict(row) for row in rows]
