"""Utilities for retrieving recipe metadata from Neo4j."""

from __future__ import annotations

from typing import Any, Dict

try:
    from langchain.tools import tool as _tool_decorator
except ImportError:  # pragma: no cover - optional dependency for LangChain integrations
    _tool_decorator = None

from recipe_wrangler.utils.neo4j_utils import run_query

try:
    from recipe_wrangler.tools.rule_based_recipe_similarity_search import (
        rule_based_search_tool,
    )
except ImportError:  # pragma: no cover - optional dependency for similarity tool
    rule_based_search_tool = None


_RECIPE_INFO_QUERY = """
MATCH (r:Recipe)
WHERE {match_predicate}

// -------- ingredients (format decimals -> common fractions) --------
OPTIONAL MATCH (r)-[rel:HAS_INGREDIENT]->(i:Ingredient)
WITH r, i, rel,
    COALESCE(rel.measurement, '') AS meas,
    split(COALESCE(rel.measurement, ''), ' ') AS parts
WITH r, i, meas,
    toFloat(CASE WHEN size(parts) > 0 THEN parts[0] ELSE NULL END) AS qty,
    reduce(out = '', x IN CASE WHEN size(parts) > 1 THEN parts[1..] ELSE [] END |
        out + CASE WHEN out = '' THEN '' ELSE ' ' END + x) AS unit
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

// -------- tags (names only) --------
OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
WITH r, ingredients, collect(DISTINCT t.name) AS tags

// -------- allergens (names only) --------
OPTIONAL MATCH (r)-[:HAS_ALLERGEN]->(a:Allergen)
WITH r, ingredients, tags, collect(DISTINCT a.name) AS allergens

RETURN r AS recipe, ingredients, tags, allergens
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
        "id": recipe_node.element_id,
        "title": recipe_props.get("title"),
        "ingredients": record.get("ingredients") or [],
        "instructions": instructions,
        "duration": recipe_props.get("duration"),
        "serves": recipe_props.get("serves"),
        "total_carbs_g_per_serving": recipe_props.get("totalcarbohydrategperserving"),
        "nutri_score": recipe_props.get("nutriscore"),
        "total_protein_g_per_serving": recipe_props.get("totalproteingperserving"),
        "total_sustainability_per_serving": recipe_props.get("totalsustainabilityperserving"),
        "total_kcal_per_serving": recipe_props.get("totalenergyfromfatkcalperserving"),
        "total_fat_g_per_serving": recipe_props.get("totalfatgperserving"),
        "total_sugar_g_per_serving": recipe_props.get("totalsugargperserving"),
        "total_fiber_g_per_serving": recipe_props.get("totaldietaryfibergperserving"),
        "total_cholesterol_mg_per_serving": recipe_props.get("totalcholesterolmgperserving"),
        "tags": record.get("tags") or [],
        "allergens": record.get("allergens") or [],
        "similar_recipes": [],
    }


def fetch_recipe_info(recipe_title: str | None = None, recipe_id: str | None = None) -> Dict[str, Any]:
    """Fetch recipe metadata by title or Neo4j element id."""

    if recipe_id is not None:
        match_predicate = "elementId(r) = $recipe_id"
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
    title = recipe.get("title")

    if title and rule_based_search_tool is not None:
        try:
            similarity_result = rule_based_search_tool.invoke(title)
        except Exception:
            recipe["similar_recipes"] = []
        else:
            if isinstance(similarity_result, dict):
                recipe["similar_recipes"] = similarity_result.get("similar_recipes") or []
            else:
                recipe["similar_recipes"] = []

    return recipe


def fetch_recipe_info_by_id(recipe_id: str) -> Dict[str, Any]:
    """Fetch recipe metadata by Neo4j element id."""

    return fetch_recipe_info(recipe_id=recipe_id)


if _tool_decorator is not None:
    fetch_recipe_info_tool = _tool_decorator(fetch_recipe_info)
else:  # pragma: no cover - runtime fallback when LangChain isn't installed
    fetch_recipe_info_tool = None


if __name__ == "__main__":
    info = fetch_recipe_info("No Knead French Bread")
    from pprint import pprint

    pprint(info)
