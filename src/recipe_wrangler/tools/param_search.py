"""Recipe search using explicit filter parameters."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import run_query


_SOURCE_NORM_EXPR = 'toLower(trim(coalesce(toString(r.source), "")))'
_CANONICAL_SOURCE_EXPR = (
    "CASE "
    f'WHEN {_SOURCE_NORM_EXPR} IN ["irish_safefood", "safefood", "irish safefood"] THEN "irish_safefood" '
    f"ELSE {_SOURCE_NORM_EXPR} "
    "END"
)
_FACET_SOURCE_EXPR = (
    "CASE "
    f'WHEN {_CANONICAL_SOURCE_EXPR} = "" THEN "unknown" '
    f"ELSE {_CANONICAL_SOURCE_EXPR} "
    "END"
)

_STABLE_RECIPE_SORT_FIELDS = f"""
      CASE WHEN coalesce(r.expert_recipe, false) THEN 0 ELSE 1 END AS _sort_expert,
      CASE
        WHEN {_CANONICAL_SOURCE_EXPR} = "healthyfoods" THEN 0
        WHEN {_CANONICAL_SOURCE_EXPR} = "foodhero" THEN 1
        WHEN {_CANONICAL_SOURCE_EXPR} = "myplate" THEN 2
        WHEN {_CANONICAL_SOURCE_EXPR} = "irish_safefood" THEN 3
        WHEN {_CANONICAL_SOURCE_EXPR} = "recipe1m" THEN 4
        ELSE 5
      END AS _sort_source,
      CASE WHEN coalesce(r.has_profile, false) THEN 0 ELSE 1 END AS _sort_profile,
      CASE WHEN r.duration IS NOT NULL AND r.serves IS NOT NULL THEN 0 ELSE 1 END AS _sort_complete,
      coalesce(toLower(r.title), "") AS _sort_title,
      coalesce(toString(r.recipe_id), toString(r.id), "") AS _sort_id,
      {_CANONICAL_SOURCE_EXPR} AS _sort_source_name,
      coalesce(toString(r.source_id), "") AS _sort_source_id,
      elementId(r) AS _sort_element_id
"""

_STABLE_RECIPE_ORDER_BY = """
    ORDER BY _sort_expert, _sort_source, _sort_profile, _sort_complete,
             _sort_title, _sort_id, _sort_source_name, _sort_source_id, _sort_element_id
"""


_SORT_FIELD_PREFIX = "_sort_"


def _strip_sort_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith(_SORT_FIELD_PREFIX)}


def _normalize_terms(items: list[str]) -> list[str]:
    cleaned = [item.strip().casefold() for item in items if str(item).strip()]
    # Preserve first occurrence order while de-duplicating.
    return list(dict.fromkeys(cleaned))


def _normalize_sources(items: list[str]) -> list[str]:
    canonicalized: list[str] = []
    seen: set[str] = set()
    for item in _normalize_terms(items):
        canonical = (
            "irish_safefood"
            if item in {"irish_safefood", "safefood", "irish safefood"}
            else item
        )
        if canonical not in seen:
            seen.add(canonical)
            canonicalized.append(canonical)
    return canonicalized


def _order_by_clause(sort_by: str | None) -> str:
    order_by_clause = _STABLE_RECIPE_ORDER_BY
    if sort_by == "random":
        order_by_clause = "ORDER BY rand()"
    elif sort_by == "title_asc":
        order_by_clause = "ORDER BY toLower(r.title) ASC"
    elif sort_by == "title_desc":
        order_by_clause = "ORDER BY toLower(r.title) DESC"
    elif sort_by == "time_asc":
        order_by_clause = "ORDER BY coalesce(toInteger(r.duration), 999999) ASC"
    elif sort_by == "time_desc":
        order_by_clause = "ORDER BY coalesce(toInteger(r.duration), 0) DESC"
    return order_by_clause


def _build_where_clause(
    filters: RecipeSearchFilters,
    *,
    extra_predicates: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    include_ingredients = _normalize_terms(filters.include_ingredients)
    exclude_ingredients = _normalize_terms(filters.exclude_ingredients)
    exclude_allergens = _normalize_terms(filters.exclude_allergens)
    diet_tags = _normalize_terms(filters.diet_tags)
    sources = _normalize_sources(filters.sources)
    dish_types = _normalize_terms(filters.dish_types)
    limit = max(1, min(int(filters.limit), 100))
    offset = max(0, int(filters.offset))

    predicates: list[str] = list(extra_predicates or [])
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
            "ALL(d IN $diet_tags WHERE EXISTS { "
            "MATCH (r)-[:HAS_TAG]->(t:Tag) "
            "WHERE toLower(t.name) = d "
            "})"
        )
        params["diet_tags"] = diet_tags

    if sources:
        predicates.append(f"{_CANONICAL_SOURCE_EXPR} IN $sources")
        params["sources"] = sources

    if dish_types:
        predicates.append(
            "EXISTS { "
            "MATCH (r)-[:HAS_TAG]->(dt:Tag) "
            "WHERE dt.category = 'dish-type' "
            "AND toLower(dt.name) IN $dish_types "
            "}"
        )
        params["dish_types"] = dish_types

    if filters.max_duration_minutes is not None:
        predicates.append(
            "coalesce(toInteger(r.duration), 999999) <= $max_duration_minutes"
        )
        params["max_duration_minutes"] = int(filters.max_duration_minutes)

    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""
    return where_clause, params


def _build_result_query(where_clause: str, order_by_clause: str) -> str:
    return f"""
    MATCH (r:Recipe)
    {where_clause}
    WITH r,
      coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
      r.title AS title,
      r.source AS source,
      r.source_id AS source_id,
      r.image_url AS image_url,
      r.duration AS duration,
      r.serves AS serves,
      coalesce(r.nutriscore, null) AS nutri_score,
      coalesce(r.totalsustainabilityperserving, null) AS sust_score,
      coalesce(r.expert_recipe, false) AS expert_recipe,
      {_STABLE_RECIPE_SORT_FIELDS}
    {order_by_clause}
    SKIP $offset
    LIMIT $limit
    OPTIONAL MATCH (r)-[:HAS_TAG]->(dt:Tag)
      WHERE dt.category = 'dish-type'
    WITH recipe_id, title, source, source_id, image_url, duration, serves,
         nutri_score, sust_score, expert_recipe,
         [n IN collect(DISTINCT dt.name) WHERE n IS NOT NULL AND trim(toString(n)) <> ""] AS dish_types
    RETURN
      recipe_id,
      title,
      source,
      source_id,
      image_url,
      duration,
      serves,
      nutri_score,
      sust_score,
      expert_recipe,
      dish_types
    """


def _build_facet_query(where_clause: str) -> str:
    return f"""
    MATCH (r:Recipe)
    {where_clause}
    RETURN 'source' AS category, {_FACET_SOURCE_EXPR} AS tag, count(r) AS count
    UNION ALL
    MATCH (r:Recipe)
    {where_clause}
    MATCH (r)-[:HAS_TAG]->(t:Tag)
    RETURN coalesce(t.category, 'uncategorized') AS category, toLower(t.name) AS tag, count(DISTINCT r) AS count
    """


def _build_count_query(where_clause: str) -> str:
    return f"""
    MATCH (r:Recipe)
    {where_clause}
    RETURN count(r) AS total
    """


def _run_count(query: str, params: dict[str, Any]) -> int:
    rows = run_query(query, params)
    if not rows:
        return 0
    return int(rows[0].get("total", 0) or 0)


def _collect_facets(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    facets: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = str(row.get("category") or "").strip()
        tag = str(row.get("tag") or "").strip()
        if not category or not tag:
            continue
        facets.setdefault(category, {})[tag] = row.get("count", 0)
    return facets


def build_param_search_cypher(
    filters: RecipeSearchFilters,
) -> tuple[str, str | None, dict[str, Any]]:
    """Build a parameterized Cypher query from explicit filters."""

    where_clause, params = _build_where_clause(filters)
    order_by_clause = _order_by_clause(filters.sort_by)

    query = _build_result_query(where_clause, order_by_clause)
    facet_query = None
    if filters.include_facets:
        facet_query = _build_facet_query(where_clause)

    return query, facet_query, params


def _has_no_constraints(filters: RecipeSearchFilters) -> bool:
    return (
        not _normalize_terms(filters.include_ingredients)
        and not _normalize_terms(filters.exclude_ingredients)
        and not _normalize_terms(filters.exclude_allergens)
        and not _normalize_terms(filters.diet_tags)
        and not _normalize_sources(filters.sources)
        and not _normalize_terms(filters.dish_types)
        and filters.max_duration_minutes is None
    )


def search_recipes_by_params(filters: RecipeSearchFilters) -> dict[str, Any]:
    """Execute parameter-based recipe search."""

    if _has_no_constraints(filters):
        where_clause, params = _build_where_clause(
            filters,
            extra_predicates=["coalesce(r.has_profile, false) = true"],
        )
        order_by_clause = _order_by_clause(filters.sort_by)

        # Unconstrained browse: stable, paginatable profile-first recipe catalog.
        # Unprofiled recipe1m recipes are nearly unreachable via browse.
        rows = run_query(_build_result_query(where_clause, order_by_clause), params)
        total = _run_count(_build_count_query(where_clause), params)
        facets = {}
        if filters.include_facets:
            facets = _collect_facets(run_query(_build_facet_query(where_clause), params))
        return {
            "results": [_strip_sort_fields(dict(row)) for row in rows],
            "facets": facets,
            "total": total,
        }

    query, facet_query, params = build_param_search_cypher(filters)
    rows = run_query(query, params)
    where_clause, _ = _build_where_clause(filters)
    total = _run_count(_build_count_query(where_clause), params)
    facets = {}
    if facet_query:
        facets = _collect_facets(run_query(facet_query, params))
    return {
        "results": [_strip_sort_fields(dict(row)) for row in rows],
        "facets": facets,
        "total": total,
    }


def warmup() -> None:
    """Prime the Neo4j driver pool and Cypher plan cache for /param_search.

    First request cost (~1.2s) is dominated by Bolt handshake + query
    compilation of the nested-CASE ordering. Running the exact query shapes
    once at startup collapses subsequent requests to their steady-state
    (~30ms) cost.
    """
    filters = RecipeSearchFilters(limit=1, offset=0, include_facets=True)
    where_clause, params = _build_where_clause(
        filters,
        extra_predicates=["coalesce(r.has_profile, false) = true"],
    )
    order_by_clause = _order_by_clause(None)
    run_query(_build_result_query(where_clause, order_by_clause), params)
    run_query(_build_facet_query(where_clause), params)
    run_query(_build_count_query(where_clause), params)
