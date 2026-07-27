"""Deterministic Elasticsearch recipe retrieval.

Takes structured search constraints, builds an Elasticsearch ``bool`` query,
and returns recipe cards. Natural-language constraint extraction is handled
separately by the API's Neo4j-independent recipe constraint extractor.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import requests

from recipe_wrangler.api.config import get_settings

_VALID_REGIONS = {"eu", "ie", "hu"}

# Region-agnostic fields returned per hit. The region-specific nutri fields
# (nutri_score_<r> / nutri_color_<r>) are appended per request.
_BASE_SOURCE_FIELDS = [
    "id", "title", "url", "source", "source_id", "image_url",
    "duration", "serves", "cost_category", "sust_score", "expert_recipe",
]


def _resolve_region(value: str) -> str:
    """Normalize a region selector (EU/IE/HU, any case) to eu/ie/hu; default eu."""
    region = (value or "eu").strip().lower()
    return region if region in _VALID_REGIONS else "eu"


@dataclass
class RecipeSearchConstraints:
    """Structured constraints consumed by Elasticsearch recipe search."""

    include_ingredients: list[str] = field(default_factory=list)
    exclude_ingredients: list[str] = field(default_factory=list)
    exclude_allergens: list[str] = field(default_factory=list)
    diet_tags: list[str] = field(default_factory=list)
    dish_types: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)
    title_query: str | None = None
    max_duration_minutes: int | None = None
    min_servings: int | None = None
    limit: int = 10
    offset: int = 0
    region: str = "eu"  # which region's nutri score the card returns


# Maps generic/UI allergen labels to canonical ES allergen identifiers.
# ES stores the labels written by detect_allergens_from_names / tag_allergens.py.
_ALLERGEN_ALIASES: dict[str, list[str]] = {
    "nut": ["peanut", "tree_nut"],
    "nuts": ["peanut", "tree_nut"],
    "peanuts": ["peanut"],
    "tree nuts": ["tree_nut"],
    "tree_nuts": ["tree_nut"],
    "shellfish": ["crustacean_shellfish", "molluscs"],
    "crustacean": ["crustacean_shellfish"],
    "dairy": ["milk"],
    "lactose": ["milk"],
    "gluten": ["gluten", "wheat"],
}


def _expand_allergens(labels: list[str]) -> list[str]:
    """Expand generic allergen labels to canonical ES identifiers, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        expanded = _ALLERGEN_ALIASES.get(label, [label])
        for canonical in expanded:
            if canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
    return out


def _norm(items: list[str]) -> list[str]:
    """Lowercase, strip, de-duplicate while preserving order."""
    cleaned = [s.strip().lower() for s in items if str(s).strip()]
    return list(dict.fromkeys(cleaned))


def normalize_recipe_title(value: object) -> str:
    """Normalize a title for exact matching without losing non-Latin letters."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").strip()).casefold()
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    words = "".join(char if char.isalnum() else " " for char in without_marks)
    return " ".join(words.split())


def _title_should_queries(title_query: str) -> list[dict[str, Any]]:
    """Build descending-confidence title clauses for a submitted search."""
    normalized = normalize_recipe_title(title_query)
    clauses: list[dict[str, Any]] = []
    if normalized:
        clauses.append(
            {"term": {"title_normalized": {"value": normalized, "boost": 100}}}
        )
    clauses.extend(
        [
            {
                "term": {
                    "title.kw": {
                        "value": title_query,
                        "case_insensitive": True,
                        "boost": 80,
                    }
                }
            },
            {"match_phrase": {"title": {"query": title_query, "boost": 20}}},
            {
                "match_phrase_prefix": {
                    "title": {"query": title_query, "boost": 10}
                }
            },
            {
                "match": {
                    "title": {
                        "query": title_query,
                        "operator": "and",
                        "boost": 5,
                    }
                }
            },
            {
                "match": {
                    "title": {
                        "query": title_query,
                        "operator": "and",
                        "fuzziness": "AUTO",
                        "prefix_length": 0,
                        "max_expansions": 50,
                        "boost": 1,
                    }
                }
            },
        ]
    )
    return clauses


def build_es_query(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Translate constraints into an Elasticsearch search body.

    Hard constraints go in `filter` context (no scoring, cached bitsets).
    Title keywords go in ``must`` as AND constraints.
    """
    filter_: list[dict] = []
    must: list[dict] = []
    must_not: list[dict] = []
    should: list[dict] = []

    # Only profiled recipes — those carrying a stored EU (global) nutrition
    # profile. EU is the global composition pool, so an EU nutri score is the
    # marker that a recipe has been profiled.
    filter_.append({"exists": {"field": "nutri_score_eu"}})

    # Included ingredients — every term must match (AND).
    for ing in _norm(c.include_ingredients):
        filter_.append({"match_phrase": {"ingredients": ing}})

    # Excluded ingredients — none may match.
    for ing in _norm(c.exclude_ingredients):
        must_not.append({"match_phrase": {"ingredients": ing}})

    # Allergens — exclude any recipe carrying one of them.
    # Expand generic labels (e.g. "nut" → ["peanut", "tree_nut"]) before matching.
    allergens = _expand_allergens(_norm(c.exclude_allergens))
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

    title_query = str(c.title_query or "").strip()
    if title_query:
        should.extend(_title_should_queries(title_query))

    limit = max(1, min(int(c.limit), 100))
    offset = max(0, int(c.offset))
    region = _resolve_region(c.region)

    bool_query: dict[str, Any] = {
        "filter": filter_,
        "must": must,
        "must_not": must_not,
    }
    if should:
        bool_query["should"] = should
        bool_query["minimum_should_match"] = 1

    # Title searches must rank by textual relevance first. General parameter
    # searches retain the established expert/curated ordering.
    if title_query:
        sort: list[Any] = [
            "_score",
            {"expert_recipe": "desc"},
            {"source_rank": "asc"},
            {"has_profile": "desc"},
            {"title.kw": "asc"},
            {"id": "asc"},
        ]
    else:
        sort = [
            {"expert_recipe": "desc"},
            {"source_rank": "asc"},
            {"has_profile": "desc"},
            "_score",
            {"title.kw": "asc"},
            {"id": "asc"},
        ]

    return {
        "from": offset,
        "size": limit,
        "_source": _BASE_SOURCE_FIELDS + [f"nutri_score_{region}", f"nutri_color_{region}"],
        "track_total_hits": True,
        "query": {"bool": bool_query},
        "sort": sort,
    }


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
        "cost_category": src.get("cost_category"),
        "nutri_score": src.get(f"nutri_score_{region}"),
        "nutri_color": src.get(f"nutri_color_{region}"),
        "sust_score": src.get("sust_score"),
        "expert_recipe": bool(src.get("expert_recipe", False)),
    }


def _rerank_title_hits(
    hits: list[dict[str, Any]],
    title_query: str,
) -> list[dict[str, Any]]:
    """Prefer the closest complete title after Elasticsearch candidate recall."""
    query = normalize_recipe_title(title_query)
    if not query:
        return hits

    def rank(hit: dict[str, Any]) -> tuple[bool, bool, float, float]:
        title = normalize_recipe_title((hit.get("_source") or {}).get("title"))
        exact = title == query
        prefix = title.startswith(query) or query.startswith(title)
        similarity = SequenceMatcher(None, query, title).ratio()
        return exact, prefix, similarity, float(hit.get("_score") or 0.0)

    return sorted(hits, key=rank, reverse=True)


def search_recipes_es(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Execute an ES recipe search. Returns results, total hits, and latency."""
    settings = get_settings()
    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    body = build_es_query(c)
    result_limit = max(1, min(int(c.limit), 100))
    result_offset = max(0, int(c.offset))
    title_query = str(c.title_query or "").strip()
    if title_query:
        # Retrieve a wider lexical pool, then use normalized string similarity
        # to prevent longer fuzzy matches from outranking the intended title.
        body["from"] = 0
        body["size"] = min(100, max(50, result_offset + result_limit))

    start = time.perf_counter()
    resp = requests.post(url, json=body, timeout=settings.elastic_timeout)
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    payload = resp.json()

    hits = payload.get("hits", {})
    result_hits = hits.get("hits", [])
    if title_query:
        result_hits = _rerank_title_hits(result_hits, title_query)
        result_hits = result_hits[result_offset : result_offset + result_limit]
    region = _resolve_region(c.region)
    return {
        "results": [_hit_to_card(h, region) for h in result_hits],
        "total": hits.get("total", {}).get("value", 0),
        "elapsed_ms": round(elapsed_ms, 1),
        "es_took_ms": payload.get("took"),
    }
