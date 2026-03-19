"""Neo4j repository adapter for recipe-centric reads."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.utils.neo4j_utils import run_query


def fetch_recipe_scores_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id,
           r.nutriscore AS nutri_score,
           r.totalsustainabilityperserving AS sust_score,
           r.duration AS duration,
           r.serves AS serves,
           r.source AS source,
           r.title AS title
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): record
        for record in rows
        if record.get("recipe_id") is not None
    }


def fetch_recipe_image_urls_by_ids(ids: list[str]) -> dict[str, str | None]:
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id, r.image_url AS image_url
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): record.get("image_url")
        for record in rows
        if record.get("recipe_id") is not None
    }
