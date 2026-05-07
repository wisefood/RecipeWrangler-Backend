"""Utilities for retrieving recipe metadata from Neo4j."""

from __future__ import annotations

import re
from typing import Any, Dict

try:
    from langchain.tools import tool as _tool_decorator
except ImportError:  # pragma: no cover - optional dependency for LangChain integrations
    _tool_decorator = None

from recipe_wrangler.utils.neo4j_utils import run_query



_RECIPE_INFO_QUERY = """
// Purpose: Fetch recipe metadata from Neo4j (title, ingredients, instructions, duration, serves, tags).

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
WITH r, i, meas, unit,
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
WITH r, i, meas, unit, pretty
ORDER BY i.name
WITH r, collect({
    name: i.name,
    quantity: CASE WHEN meas = '' THEN NULL ELSE meas END,
    unit: CASE WHEN unit = '' THEN NULL ELSE unit END,
    measurement: pretty
}) AS ingredients
OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
WITH r, ingredients, collect(distinct t.name) AS raw_tags
RETURN
  r AS recipe,
  ingredients,
  [tag IN raw_tags WHERE tag IS NOT NULL AND trim(toString(tag)) <> ""] AS tags
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

    raw_tags = record.get("tags")
    tags = []
    if isinstance(raw_tags, list):
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]

    raw_ingredients = record.get("ingredients") or []
    ingredients: list[dict[str, Any]] = []
    for item in raw_ingredients:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        quantity = normalized.get("quantity")
        if isinstance(quantity, str):
            q_text = quantity.strip()
            # Some rows persist "measurement" as "1.0 tbsp". Keep quantity numeric when possible.
            match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\b", q_text)
            if match:
                normalized["quantity"] = match.group(1)
        ingredients.append(normalized)

    return {
        "recipe_id": recipe_props.get("recipe_id") or recipe_props.get("id"),
        "title": recipe_props.get("title"),
        "url": recipe_props.get("url"),
        "source": recipe_props.get("source"),
        "source_id": recipe_props.get("source_id"),
        "expert_recipe": bool(recipe_props.get("expert_recipe", False)),
        "image_url": recipe_props.get("image_url"),
        "edited": recipe_props.get("edited"),
        "tags": tags,
        "ingredients": ingredients,
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
        # Some graphs store `id` as numeric while API calls provide string ids.
        # Compare via toString() so by-id lookups work across both schemas.
        match_predicate = "toString(r.recipe_id) = $recipe_id OR toString(r.id) = $recipe_id"
        params = {"recipe_id": str(recipe_id)}
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
