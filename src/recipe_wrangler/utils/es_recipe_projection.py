"""Runtime projection of a single recipe into the recipes_v2 search index.

The offline builder (scripts/elasticsearch/index_recipes_v2.py) owns the index
mapping and the corpus-wide rebuild; this module is its per-recipe runtime
counterpart so that create/update endpoints keep recipes_v2 fresh instead of
serving stale docs until the next full rebuild. The document shape and field
cleaning MUST stay in lockstep with the builder — it always writes the full
document (index, not partial update), so any drift self-heals on the next
write to that recipe.
"""

from __future__ import annotations

import logging
from typing import Any

from recipe_wrangler.utils.http_pool import get_http_session
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.utils.nutrition_postgres import fetch_recipe_region_scores

logger = logging.getLogger(__name__)

# Mirrors CURATED_SOURCES in the offline builder.
_CURATED_SOURCES = {"foodhero", "healthyfoods"}

# Single-recipe variant of the builder's corpus QUERY.
_RECIPE_QUERY = """
MATCH (r:Recipe)
WHERE r.recipe_id = $rid OR r.id = $rid
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
  RETURN collect(DISTINCT i.name) AS ingredients
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
  RETURN collect(DISTINCT al.name) AS allergens
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
  RETURN collect(DISTINCT t.name) AS tags,
         collect(DISTINCT CASE WHEN t.category = 'dish-type' THEN t.name END) AS dish_types
}
RETURN
  coalesce(toString(r.recipe_id), toString(r.id)) AS id,
  coalesce(toString(r.title), "") AS title,
  coalesce(toString(r.url), "") AS url,
  coalesce(toString(r.image_url), "") AS image_url,
  coalesce(toString(r.source), "") AS source,
  coalesce(toString(r.source_id), "") AS source_id,
  r.duration AS duration,
  r.serves AS serves,
  coalesce(toString(r.cost_category), "") AS cost_category,
  coalesce(r.expert_recipe, false) AS expert_recipe,
  coalesce(toString(r.status), "active") AS status,
  coalesce(r.has_profile, false) AS has_profile,
  coalesce(r.has_rcsi_lab_nutrition, false) AS has_rcsi_nutrition,
  coalesce(r.has_planeat_nutrition, false) AS has_planeat_nutrition,
  coalesce(toString(r.ground_truth_nutrition_source), "") AS ground_truth_nutrition_source,
  ingredients, allergens, tags, dish_types
LIMIT 1
"""


def _clean_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        item = _clean_str(v).lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_recipe_v2_doc(recipe_id: str) -> dict[str, Any] | None:
    """Assemble the full recipes_v2 document for one recipe from its owners
    (Neo4j content/graph + Postgres scores). None if the recipe is unknown."""
    rows = run_query(_RECIPE_QUERY, {"rid": str(recipe_id)})
    if not rows:
        return None
    row = rows[0]

    source = _clean_str(row.get("source"))
    doc: dict[str, Any] = {
        "id": _clean_str(row.get("id")),
        "title": _clean_str(row.get("title")),
        "url": _clean_str(row.get("url")),
        "image_url": _clean_str(row.get("image_url")),
        "source": source,
        "source_id": _clean_str(row.get("source_id")),
        "source_rank": 0 if source.lower() in _CURATED_SOURCES else 1,
        "ingredients": _clean_list(row.get("ingredients")),
        "allergens": _clean_list(row.get("allergens")),
        "tags": _clean_list(row.get("tags")),
        "dish_types": _clean_list(row.get("dish_types")),
        "duration": _to_float(row.get("duration")),
        "serves": _to_float(row.get("serves")),
        "cost_category": _clean_str(row.get("cost_category")) or None,
        "nutri_score_us": None, "nutri_color_us": None,
        "nutri_score_ie": None, "nutri_color_ie": None,
        "nutri_score_hu": None, "nutri_color_hu": None,
        "nutri_score_eu": None, "nutri_color_eu": None,
        "sust_score": None,
        "expert_recipe": bool(row.get("expert_recipe")),
        "status": _clean_str(row.get("status")) or "active",
        "has_profile": bool(row.get("has_profile")),
        "has_rcsi_nutrition": bool(row.get("has_rcsi_nutrition")),
        "has_planeat_nutrition": bool(row.get("has_planeat_nutrition")),
        "ground_truth_nutrition_source": _clean_str(row.get("ground_truth_nutrition_source")),
    }

    scores = fetch_recipe_region_scores(doc["id"])
    for region in ("us", "ie", "hu", "eu"):
        region_score = scores.get(region)
        if region_score:
            doc[f"nutri_score_{region}"] = region_score["nutri_score"]
            doc[f"nutri_color_{region}"] = region_score["nutri_color"]
    doc["sust_score"] = scores.get("sust_score")

    return doc


def project_recipe_to_es_v2(
    recipe_id: str,
    *,
    es_url: str,
    index: str,
    timeout: float = 10,
) -> bool:
    """Write the full, freshly assembled recipes_v2 doc for one recipe.

    Best-effort by contract: returns False (and logs) on any failure — the
    owners (Neo4j/Postgres) already hold the truth and the offline rebuild or
    the next write to this recipe converges the index.
    """
    try:
        doc = build_recipe_v2_doc(recipe_id)
        if doc is None or not doc["id"]:
            logger.warning("recipes_v2 projection: recipe %s not found in Neo4j", recipe_id)
            return False
        resp = get_http_session().put(
            f"{es_url}/{index}/_doc/{doc['id']}",
            json=doc,
            timeout=timeout,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.warning("recipes_v2 projection failed for %s", recipe_id, exc_info=True)
        return False
