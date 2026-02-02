"""Utilities for retrieving recipe metadata from Neo4j."""

from __future__ import annotations

from typing import Any, Dict

try:
    from langchain.tools import tool as _tool_decorator
except ImportError:  # pragma: no cover - optional dependency for LangChain integrations
    _tool_decorator = None

from recipe_wrangler.utils.neo4j_utils import run_query



_RECIPE_INFO_QUERY = """
// Purpose: Fetch recipe metadata from Neo4j (title, ingredients, instructions, duration, serves).

MATCH (r:Recipe)
WHERE {match_predicate}

// -------- ingredients (format decimals -> common fractions) --------
OPTIONAL MATCH (r)-[rel:HAS_INGREDIENT]->(i:Ingredient)
WITH r, i, rel,
    COALESCE(rel.measurement, '') AS meas,
    COALESCE(rel.unit, '') AS unit
WITH r, i, meas, unit,
    toFloat(CASE WHEN meas = '' THEN NULL ELSE meas END) AS qty
WITH r, i, meas, unit, qty,
    CASE WHEN qty IS NULL THEN NULL ELSE toInteger(floor(qty)) END AS whole,
    CASE WHEN qty IS NULL THEN NULL ELSE (qty - toInteger(floor(qty))) END AS frac
WITH r, i, meas, unit, whole,
    CASE
        WHEN frac IS NULL THEN NULL
        WHEN abs(frac - 0.125) < 0.01 THEN '1/8'
        WHEN abs(frac - 0.25)  < 0.01 THEN '1/4'
        WHEN abs(frac - 0.333) < 0.02 THEN '1/3'
        WHEN abs(frac - 0.5)   < 0.01 THEN '1/2'
        WHEN abs(frac - 0.666) < 0.02 THEN '2/3'
        WHEN abs(frac - 0.75)  < 0.01 THEN '3/4'
        ELSE NULL
    END AS fracTxt
WITH r, i,
    CASE
        WHEN fracTxt IS NULL AND (whole IS NULL OR unit = '') THEN meas
        ELSE trim(
            CASE
                WHEN fracTxt IS NULL THEN toString(whole)
                WHEN whole = 0 THEN fracTxt
                ELSE toString(whole) + ' ' + fracTxt
            END + CASE WHEN unit = '' THEN '' ELSE ' ' + unit END
        )
    END AS pretty
WITH r, i, pretty
ORDER BY i.name
WITH r, collect({name: i.name, measurement: pretty}) AS ingredients
RETURN r AS recipe, ingredients
"""


def _record_to_recipe_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    recipe_node = record["recipe"]
    recipe_props = dict(recipe_node)

    raw_instructions = recipe_props.get("instructions")
    if isinstance(raw_instructions, list):
        instructions = raw_instructions
    elif isinstance(raw_instructions, str):
        instructions = [step.strip() for step in raw_instructions.split("\n") if step.strip()]
    else:
        instructions = []

    return {
        "recipe_id": recipe_props.get("recipe_id") or recipe_props.get("id"),
        "title": recipe_props.get("title"),
        "image_url": recipe_props.get("image_url"),
        "ingredients": record.get("ingredients") or [],
        "instructions": instructions,
        "duration": recipe_props.get("duration"),
        "serves": recipe_props.get("serves"),
    }


def fetch_recipe_info(recipe_title: str | None = None, recipe_id: str | None = None) -> Dict[str, Any]:
    """Fetch recipe metadata by title or recipe_id property."""

    if recipe_id is None and recipe_title is not None:
        import re
        if re.match(r"^[0-9a-f]{10}$", recipe_title.strip()):
            recipe_id = recipe_title.strip()
            recipe_title = None

    if recipe_id is not None:
        match_predicate = "r.recipe_id = $recipe_id OR r.id = $recipe_id"
        params = {"recipe_id": recipe_id}
    elif recipe_title is not None:
        match_predicate = "toLower(r.title) = toLower($recipe_title)"
        params = {"recipe_title": recipe_title}
    else:
        return {}

    query = _RECIPE_INFO_QUERY.replace("{match_predicate}", match_predicate)
    result = run_query(query, params)
    if not result:
        return {}

    recipe = _record_to_recipe_dict(result[0])
    return recipe


def fetch_recipe_info_by_id(recipe_id: str) -> Dict[str, Any]:
    """Fetch recipe metadata by recipe_id property."""

    return fetch_recipe_info(recipe_id=recipe_id)


if _tool_decorator is not None:
    fetch_recipe_info_tool = _tool_decorator(fetch_recipe_info)
else:  # pragma: no cover - runtime fallback when LangChain isn't installed
    fetch_recipe_info_tool = None


if __name__ == "__main__":
    info = fetch_recipe_info("No Knead French Bread")
    from pprint import pprint

    pprint(info)
