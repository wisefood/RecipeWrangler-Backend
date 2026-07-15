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

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.utils.http_pool import get_http_session, post_query_with_retry
from recipe_wrangler.utils.recipe_status import es_not_disabled_clause

# Index built by scripts/elasticsearch/index_recipes_v2.py
ES_INDEX = "recipes_v2"

_VALID_REGIONS = {"eu", "ie", "hu"}

# Region-agnostic fields returned per hit. The region-specific nutri fields
# (nutri_score_<r> / nutri_color_<r>) are appended per request.
_BASE_SOURCE_FIELDS = [
    "id", "title", "url", "source", "source_id", "image_url",
    "duration", "serves", "cost_category", "sust_score", "expert_recipe",
    "status",
]


def _resolve_region(value: str) -> str:
    """Normalize a region selector (EU/IE/HU, any case) to eu/ie/hu; default eu."""
    region = (value or "eu").strip().lower()
    return region if region in _VALID_REGIONS else "eu"


@dataclass
class RecipeSearchConstraints:
    """Same constraint set the Neo4j search consumes, decoupled from Pydantic."""

    include_ingredients: list[str] = field(default_factory=list)
    exclude_ingredients: list[str] = field(default_factory=list)
    exclude_allergens: list[str] = field(default_factory=list)
    # Soft preferences (e.g. from the member profile): boost matching recipes
    # in the ranking without ever filtering non-matching ones out.
    boost_ingredients: list[str] = field(default_factory=list)
    boost_tags: list[str] = field(default_factory=list)
    diet_tags: list[str] = field(default_factory=list)
    # When True, title keywords are OR-matched (any keyword suffices) instead
    # of the default AND — used as a zero-result relaxation retry.
    title_match_any: bool = False
    sources: list[str] = field(default_factory=list)
    dish_types: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)
    max_duration_minutes: int | None = None
    min_servings: int | None = None
    limit: int = 10
    offset: int = 0
    region: str = "eu"  # which region's nutri score the card returns
    include_facets: bool = False
    sort_by: str | None = None
    include_disabled: bool = False  # console/admin: surface soft-deleted recipes


class ResultWindowExceededError(Exception):
    """offset+limit went past the index's max_result_window — a client error,
    not an Elasticsearch outage."""


# Canonical source slugs (the API/UI filter contract, matching the Neo4j
# canonicalization) mapped to the raw `source` keyword values in recipes_v2.
_SOURCE_SLUG_TO_RAW: dict[str, list[str]] = {
    "healthyfoods": ["HealthyFoods"],
    "foodhero": ["FoodHero"],
    "myplate": ["MyPlate"],
    "irish_safefood": ["Curated Irish Recipes"],
    "safefood": ["Curated Irish Recipes"],
    "irish safefood": ["Curated Irish Recipes"],
    "hungarian": ["Curated Hungarian Recipes"],
    "slovenian": ["Curated Slovenian Recipes"],
    "recipe1m": ["recipe1m"],
}

_RAW_SOURCE_TO_SLUG: dict[str, str] = {
    "healthyfoods": "healthyfoods",
    "foodhero": "foodhero",
    "myplate": "myplate",
    "curated irish recipes": "irish_safefood",
    "curated hungarian recipes": "hungarian",
    "curated slovenian recipes": "slovenian",
    "recipe1m": "recipe1m",
}

# The index carries spelling variants per dish type (main-dish/main_dish,
# desserts/dessert, snacks/snack). Filters expand the canonical value to all
# variants; facets fold variant buckets back onto the canonical key.
_DISH_TYPE_VARIANTS: dict[str, list[str]] = {
    "main-dish": ["main-dish", "main_dish"],
    "desserts": ["desserts", "dessert"],
    "snacks": ["snacks", "snack"],
}

_DISH_TYPE_CANONICAL: dict[str, str] = {
    variant: canonical
    for canonical, variants in _DISH_TYPE_VARIANTS.items()
    for variant in variants
}


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

    # Only profiled recipes — those carrying a stored EU (global) nutrition
    # profile. EU is the global composition pool, so an EU nutri score is the
    # marker that a recipe has been profiled.
    filter_.append({"exists": {"field": "nutri_score_eu"}})

    # Soft-deleted recipes are hidden everywhere; the console opts in via
    # include_disabled to find and re-enable them.
    if not c.include_disabled:
        must_not.append(es_not_disabled_clause())

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

    # Sources — recipe must come from one of them. Incoming values are
    # canonical slugs; expand to the raw keyword values stored in the index.
    sources = _norm(c.sources)
    if sources:
        raw_sources: list[str] = []
        for slug in sources:
            raw_sources.extend(_SOURCE_SLUG_TO_RAW.get(slug, [slug]))
        filter_.append({"terms": {"source": raw_sources}})

    # Dish types — recipe must match at least one (any indexed variant).
    dish_types = _norm(c.dish_types)
    if dish_types:
        expanded: list[str] = []
        for dt in dish_types:
            expanded.extend(_DISH_TYPE_VARIANTS.get(dt, [dt]))
        filter_.append({"terms": {"dish_types": expanded}})

    if c.max_duration_minutes is not None:
        filter_.append({"range": {"duration": {"lte": c.max_duration_minutes}}})

    if c.min_servings is not None:
        filter_.append({"range": {"serves": {"gte": c.min_servings}}})

    # Title keywords — fuzzy so plural/singular and small typos still match
    # ("desserts" vs "dessert"). Default: every keyword must appear (AND);
    # title_match_any relaxes to any-keyword for the zero-result retry.
    title_keywords = _norm(c.title_keywords)
    if title_keywords:
        if c.title_match_any:
            must.append({
                "match": {
                    "title": {
                        "query": " ".join(title_keywords),
                        "operator": "or",
                        "fuzziness": "AUTO",
                    }
                }
            })
        else:
            for kw in title_keywords:
                must.append({"match": {"title": {"query": kw, "fuzziness": "AUTO"}}})

    # Preference boosts — pure scoring, never filtering: a recipe matching a
    # preferred ingredient or diet tag ranks higher, one matching none still
    # qualifies. Member diet groups ride here (tag coverage in the index is
    # too sparse for them to be hard filters without emptying results).
    should: list[dict] = []
    for ing in _norm(c.boost_ingredients):
        should.append({"match_phrase": {"ingredients": {"query": ing, "boost": 2.0}}})
    for tag in _norm(c.boost_tags):
        should.append({"term": {"tags": {"value": tag, "boost": 1.5}}})

    limit = max(1, min(int(c.limit), 100))
    offset = max(0, int(c.offset))
    region = _resolve_region(c.region)

    query: dict[str, Any] = {"bool": {"filter": filter_, "must": must, "must_not": must_not}}
    if should:
        query["bool"]["should"] = should
        query["bool"]["minimum_should_match"] = 0

    # Mirror the Neo4j stable sort by default: expert first, curated sources,
    # profiled, then relevance, then title/id for determinism. Explicit
    # sort_by values override it; 'random' uses a random_score replacement.
    sort: list[Any] = [
        {"expert_recipe": "desc"},
        {"source_rank": "asc"},
        {"has_profile": "desc"},
        "_score",
        {"title.kw": "asc"},
        {"id": "asc"},
    ]
    if should:
        # Personalization boosts must be able to reorder across source ranks,
        # otherwise _score (4th key) almost never breaks a tie. Experts stay
        # pinned first.
        sort = [
            {"expert_recipe": "desc"},
            "_score",
            {"title.kw": "asc"},
            {"id": "asc"},
        ]
    if c.sort_by == "title_asc":
        sort = [{"title.kw": "asc"}, {"id": "asc"}]
    elif c.sort_by == "title_desc":
        sort = [{"title.kw": "desc"}, {"id": "asc"}]
    elif c.sort_by == "time_asc":
        sort = [{"duration": {"order": "asc", "missing": "_last"}}, {"id": "asc"}]
    elif c.sort_by == "time_desc":
        sort = [{"duration": {"order": "desc", "missing": "_last"}}, {"id": "asc"}]
    elif c.sort_by == "random":
        query = {"function_score": {"query": query, "random_score": {}, "boost_mode": "replace"}}
        sort = [{"_score": "desc"}, {"id": "asc"}]

    body: dict[str, Any] = {
        "from": offset,
        "size": limit,
        "_source": _BASE_SOURCE_FIELDS + [f"nutri_score_{region}", f"nutri_color_{region}"],
        "track_total_hits": True,
        "query": query,
        "sort": sort,
    }

    if c.include_facets:
        # Mirror the Neo4j facet categories the UI consumes ('dish-type' drives
        # the dish-type filter panel; 'source' is kept for contract parity).
        body["aggs"] = {
            "dish_types": {"terms": {"field": "dish_types", "size": 100}},
            "sources": {"terms": {"field": "source", "size": 50}},
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
        "cost_category": src.get("cost_category"),
        "nutri_score": src.get(f"nutri_score_{region}"),
        "nutri_color": src.get(f"nutri_color_{region}"),
        "sust_score": src.get("sust_score"),
        "expert_recipe": bool(src.get("expert_recipe", False)),
        "status": src.get("status") or "active",
    }


def search_recipes_es(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Execute an ES recipe search. Returns results, total hits, and latency."""
    settings = get_settings()
    url = f"{settings.elastic_url}/{ES_INDEX}/_search"
    body = build_es_query(c)

    start = time.perf_counter()
    resp = post_query_with_retry(url, body, timeout=settings.elastic_timeout)
    elapsed_ms = (time.perf_counter() - start) * 1000
    if resp.status_code == 400 and "max_result_window" in resp.text:
        raise ResultWindowExceededError(
            f"Requested page (offset {c.offset} + limit {c.limit}) is beyond the "
            "index's max_result_window; lower the offset."
        )
    resp.raise_for_status()
    payload = resp.json()

    hits = payload.get("hits", {})
    region = _resolve_region(c.region)
    out = {
        "results": [_hit_to_card(h, region) for h in hits.get("hits", [])],
        "total": hits.get("total", {}).get("value", 0),
        "elapsed_ms": round(elapsed_ms, 1),
        "es_took_ms": payload.get("took"),
    }
    if c.include_facets:
        out["facets"] = _collect_facets(payload.get("aggregations", {}))
    return out


def _collect_facets(aggregations: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Shape ES aggregation buckets into the Neo4j facet contract:
    {category: {tag: count}} with 'dish-type' (hyphen) keys.

    Tags are emitted as the same canonical values the filters accept, so the
    UI can send a facet key straight back as a filter: dish-type variants fold
    onto their canonical spelling, raw source names map to canonical slugs.
    """
    facets: dict[str, dict[str, int]] = {}

    dish_map: dict[str, int] = {}
    for b in aggregations.get("dish_types", {}).get("buckets", []):
        key = str(b.get("key", "")).strip().lower()
        if not key:
            continue
        canonical = _DISH_TYPE_CANONICAL.get(key, key)
        dish_map[canonical] = dish_map.get(canonical, 0) + int(b.get("doc_count", 0))
    if dish_map:
        facets["dish-type"] = dish_map

    source_map: dict[str, int] = {}
    for b in aggregations.get("sources", {}).get("buckets", []):
        key = str(b.get("key", "")).strip().lower()
        if not key:
            continue
        slug = _RAW_SOURCE_TO_SLUG.get(key, key)
        source_map[slug] = source_map.get(slug, 0) + int(b.get("doc_count", 0))
    if source_map:
        facets["source"] = source_map

    return facets
