"""Deterministic Elasticsearch recipe search.

Drop-in alternative to the Neo4j param/text2cypher search: takes the same
constraint set, builds an ES `bool` query, and returns recipe cards in the
same shape as `param_search.search_recipes_by_params`.

No LLM calls. Not wired into the API — exercised via `scripts/test_es_search.py`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from recipe_wrangler.api.config import get_settings

# Index built by scripts/elasticsearch/index_recipes_v2.py
ES_INDEX = "recipes_v2"

_VALID_REGIONS = {"us", "ie", "hu"}

# Region-agnostic fields returned per hit. The region-specific nutri fields
# (nutri_score_<r> / nutri_color_<r>) are appended per request.
_BASE_SOURCE_FIELDS = [
    "id", "title", "url", "source", "source_id", "image_url",
    "duration", "serves", "sust_score", "expert_recipe", "dish_types",
]


# Maps each known tag to its facet category (mirrors Tag.category in Neo4j).
_TAG_CATEGORY = {
    "nut_free": "dietary", "vegetarian": "dietary", "pescatarian": "dietary",
    "pescatarian_safe": "dietary", "dairy_free": "dietary", "vegan": "dietary",
    "gluten_free": "dietary", "gluten-free": "dietary", "high-protein": "dietary",
    "low-carb": "dietary", "low-fat": "dietary",
    "5_ingredients_or_less": "simplicity",
    "30_minutes_or_less": "time",
    "main-dish": "dish-type", "breakfast": "dish-type", "desserts": "dish-type",
    "snacks": "dish-type", "beverages": "dish-type",
}


def _resolve_region(value: str) -> str:
    """Normalize a region selector (US/IE/HU, any case) to us/ie/hu; default us."""
    region = (value or "us").strip().lower()
    return region if region in _VALID_REGIONS else "us"


@dataclass
class RecipeSearchConstraints:
    """Same constraint set the Neo4j search consumes, decoupled from Pydantic."""

    include_ingredients: list[str] = field(default_factory=list)
    exclude_ingredients: list[str] = field(default_factory=list)
    exclude_allergens: list[str] = field(default_factory=list)
    diet_tags: list[str] = field(default_factory=list)
    dish_types: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)
    max_duration_minutes: int | None = None
    min_servings: int | None = None
    limit: int = 10
    offset: int = 0
    region: str = "us"  # which region's nutri score the card returns
    include_facets: bool = False


def _norm(items: list[str]) -> list[str]:
    """Lowercase, strip, de-duplicate while preserving order."""
    cleaned = [s.strip().lower() for s in items if str(s).strip()]
    return list(dict.fromkeys(cleaned))


def build_es_query(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Translate constraints into an Elasticsearch search body.

    Hard constraints go in `filter` context (no scoring, cached bitsets).
    Title keywords go in `must` as an AND filter, mirroring text2cypher's
    ALL(word IN ... WHERE toLower(title) CONTAINS word).
    """
    filter_: list[dict] = []
    must: list[dict] = []
    must_not: list[dict] = []

    # Only profiled recipes — those carrying a stored Postgres nutrition
    # profile. Excludes unprofiled recipes that would otherwise be live-
    # profiled on the detail page from low-quality (recipe1m) source data.
    filter_.append({"exists": {"field": "nutri_score_us"}})

    # Included ingredients — every term must match (AND).
    for ing in _norm(c.include_ingredients):
        filter_.append({"match_phrase": {"ingredients": ing}})

    # Excluded ingredients — none may match.
    for ing in _norm(c.exclude_ingredients):
        must_not.append({"match_phrase": {"ingredients": ing}})

    # Allergens — exclude any recipe carrying one of them.
    allergens = _norm(c.exclude_allergens)
    if allergens:
        must_not.append({"terms": {"allergens": allergens}})

    # Diet tags — every tag must be present.
    for tag in _norm(c.diet_tags):
        filter_.append({"term": {"tags": tag}})

    # Dish types — recipe must match at least one.
    dish_types = _norm(c.dish_types)
    if dish_types:
        filter_.append({"terms": {"dish_types": dish_types}})

    if c.max_duration_minutes is not None:
        filter_.append({"range": {"duration": {"lte": c.max_duration_minutes}}})

    if c.min_servings is not None:
        filter_.append({"range": {"serves": {"gte": c.min_servings}}})

    # Title keywords — every keyword must appear in the title (AND).
    for kw in _norm(c.title_keywords):
        must.append({"match": {"title": kw}})

    limit = max(1, min(int(c.limit), 100))
    offset = max(0, int(c.offset))
    region = _resolve_region(c.region)

    body: dict[str, Any] = {
        "from": offset,
        "size": limit,
        "_source": _BASE_SOURCE_FIELDS + [f"nutri_score_{region}", f"nutri_color_{region}"],
        "track_total_hits": True,
        "query": {"bool": {"filter": filter_, "must": must, "must_not": must_not}},
        # Mirror the Neo4j stable sort: expert first, curated sources, profiled,
        # then relevance, then title/id for determinism.
        "sort": [
            {"expert_recipe": "desc"},
            {"source_rank": "asc"},
            {"has_profile": "desc"},
            "_score",
            {"title.kw": "asc"},
            {"id": "asc"},
        ],
    }
    if c.include_facets:
        # Facet counts within the filtered result set — by source and by tag.
        body["aggs"] = {
            "source": {"terms": {"field": "source", "size": 50}},
            "tags": {"terms": {"field": "tags", "size": 100}},
        }
    return body


def _hit_to_card(hit: dict, region: str) -> dict[str, Any]:
    src = hit.get("_source", {})
    return {
        "recipe_id": src.get("id"),
        "title": src.get("title"),
        "url": src.get("url") or None,
        "source": src.get("source"),
        "source_id": src.get("source_id") or None,
        "image_url": src.get("image_url") or None,
        "duration": src.get("duration"),
        "serves": src.get("serves"),
        "nutri_score": src.get(f"nutri_score_{region}"),
        "nutri_color": src.get(f"nutri_color_{region}"),
        "sust_score": src.get("sust_score"),
        "expert_recipe": bool(src.get("expert_recipe", False)),
        "dish_types": src.get("dish_types") or [],
    }


def _collect_facets(aggregations: dict) -> dict[str, dict[str, int]]:
    """Shape ES aggregations into {category: {value: count}}, like the Neo4j facets."""
    facets: dict[str, dict[str, int]] = {}
    for bucket in aggregations.get("source", {}).get("buckets", []):
        facets.setdefault("source", {})[bucket["key"]] = bucket["doc_count"]
    for bucket in aggregations.get("tags", {}).get("buckets", []):
        category = _TAG_CATEGORY.get(bucket["key"], "uncategorized")
        facets.setdefault(category, {})[bucket["key"]] = bucket["doc_count"]
    return facets


def search_recipes_es(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Execute an ES recipe search. Returns results, total hits, and latency."""
    settings = get_settings()
    url = f"{settings.elastic_url}/{ES_INDEX}/_search"
    body = build_es_query(c)

    start = time.perf_counter()
    resp = requests.post(url, json=body, timeout=settings.elastic_timeout)
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    payload = resp.json()

    hits = payload.get("hits", {})
    region = _resolve_region(c.region)
    return {
        "results": [_hit_to_card(h, region) for h in hits.get("hits", [])],
        "total": hits.get("total", {}).get("value", 0),
        "facets": _collect_facets(payload.get("aggregations", {})),
        "elapsed_ms": round(elapsed_ms, 1),
        "es_took_ms": payload.get("took"),
    }
