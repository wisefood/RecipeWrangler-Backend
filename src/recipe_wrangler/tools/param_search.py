"""Recipe search using explicit filter parameters."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.utils.neo4j_utils import run_query


_STABLE_RECIPE_SORT_FIELDS = """
      CASE WHEN coalesce(r.expert_recipe, false) THEN 0 ELSE 1 END AS _sort_expert,
      CASE WHEN toLower(coalesce(r.source, "")) IN ["foodhero", "healthyfoods"] THEN 0 ELSE 1 END AS _sort_source,
      CASE WHEN coalesce(r.has_profile, false) THEN 0 ELSE 1 END AS _sort_profile,
      CASE WHEN r.duration IS NOT NULL AND r.serves IS NOT NULL THEN 0 ELSE 1 END AS _sort_complete,
      coalesce(toLower(r.title), "") AS _sort_title,
      coalesce(toString(r.recipe_id), toString(r.id), "") AS _sort_id,
      coalesce(toString(r.source), "") AS _sort_source_name,
      coalesce(toString(r.source_id), "") AS _sort_source_id,
      elementId(r) AS _sort_element_id
"""

_STABLE_RECIPE_ORDER_BY = """
    ORDER BY _sort_expert, _sort_source, _sort_profile, _sort_complete,
             _sort_title, _sort_id, _sort_source_name, _sort_source_id, _sort_element_id
"""


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
    offset = max(0, int(filters.offset))

    predicates: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

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
      coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
      r.title AS title,
      r.source AS source,
      r.source_id AS source_id,
      r.image_url AS image_url,
      r.duration AS duration,
      r.serves AS serves,
      r.nutriscore AS nutri_score,
      r.totalsustainabilityperserving AS sust_score,
      coalesce(r.expert_recipe, false) AS expert_recipe,
      {_STABLE_RECIPE_SORT_FIELDS}
    {_STABLE_RECIPE_ORDER_BY}
    SKIP $offset
    LIMIT $limit
    """
    return query, params


def _has_no_constraints(filters: RecipeSearchFilters) -> bool:
    return (
        not _normalize_terms(filters.include_ingredients)
        and not _normalize_terms(filters.exclude_ingredients)
        and not _normalize_terms(filters.exclude_allergens)
        and not _normalize_terms(filters.diet_tags)
        and filters.max_duration_minutes is None
    )


def search_recipes_by_params(filters: RecipeSearchFilters) -> list[dict[str, Any]]:
    """Execute parameter-based recipe search."""

    if _has_no_constraints(filters):
        limit = max(1, min(int(filters.limit), 100))
        offset = max(0, int(filters.offset))

        # Unconstrained browse: stable, paginatable profile-first recipe catalog.
        # Unprofiled recipe1m recipes are nearly unreachable via browse.
        rows = run_query(
            f"""
            MATCH (r:Recipe)
            WHERE coalesce(r.has_profile, false) = true
            RETURN
              coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
              r.title AS title,
              r.source AS source,
              r.source_id AS source_id,
              r.image_url AS image_url,
              r.duration AS duration,
              r.serves AS serves,
              r.nutriscore AS nutri_score,
              r.totalsustainabilityperserving AS sust_score,
              coalesce(r.expert_recipe, false) AS expert_recipe,
              {_STABLE_RECIPE_SORT_FIELDS}
            {_STABLE_RECIPE_ORDER_BY}
            SKIP $offset
            LIMIT $limit
            """,
            {"limit": limit, "offset": offset},
        )
        return [dict(row) for row in rows]

    query, params = build_param_search_cypher(filters)
    rows = run_query(query, params)
    return [dict(row) for row in rows]
