"""Deterministic Elasticsearch recipe retrieval.

Takes structured search constraints, builds an Elasticsearch ``bool`` query,
and returns recipe cards. Natural-language constraint extraction is handled
separately by the API's Neo4j-independent recipe constraint extractor.
"""

from __future__ import annotations

import time
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.catalog import diets as D
from recipe_wrangler.catalog.sources import SOURCES as _REGISTERED_SOURCES
from recipe_wrangler.catalog.sources import canonical_course_type
from recipe_wrangler.utils.http_pool import get_http_session, post_query_with_retry
from recipe_wrangler.utils.recipe_status import es_not_disabled_clause

logger = logging.getLogger(__name__)

_REGION_ALIASES = {
    "eu": "eu",
    "ie": "ie",
    "irish": "ie",
    "hu": "hu",
    "hungarian": "hu",
    "si": "slovenian",
    "slovenian": "slovenian",
}
# Deprecated compatibility name. Keep it on the live alias, never on a concrete
# generation: recipes_v4 contains the annotation history that recipes_v2 lacks.
ES_INDEX = "recipes"

# Region-agnostic fields returned per hit. The region-specific nutri fields
# (nutri_score_<r> / nutri_color_<r>) are appended per request.
_BASE_SOURCE_FIELDS = [
    # `recipe_id` first: in recipes_v2 the business identifier lived in `id`,
    # but the catalog index follows the entity convention where `id` is a
    # generated UUID and `recipe_id` holds the source identifier. Reading `id`
    # alone would hand the UI UUIDs and break every recipe link after the flip.
    "id", "recipe_id", "title", "url", "source", "source_id", "image_url",
    "duration", "serves", "cost_category", "sust_score", "expert_recipe",
    "status", "allergens", "allergen_evidence", "suitable_for",
    "consumer_suitability",
    # dish_types was filterable but never returned, so a client could narrow by
    # category and get nothing back to render or group by — half of why
    # browse-by-category could not work. `course_types` is the recipes_v3 name;
    # requesting a field an index lacks is a no-op in Elasticsearch, so both can
    # be asked for across the v2/v3 transition.
    "dish_types", "course_types",
    # Annotation facets. Filterable and aggregated long before they were
    # returned, which left cards unable to show *why* a recipe matched — a
    # cuisine chip in the sidebar with no cuisine on the result it produced.
    "cuisines", "moods", "flavor_profiles", "food_groups",
    "convenience", "nutrition_claims",
]

_SUPPORTED_CONSUMER_GROUPS = {"vegan", "vegetarian"}


def _resolve_region(value: str) -> str:
    """Normalize an API region selector to its Elasticsearch field suffix."""
    region = (value or "eu").strip().lower()
    return _REGION_ALIASES.get(region, "eu")


@dataclass
class RecipeSearchConstraints:
    """Structured constraints consumed by Elasticsearch recipe search."""

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
    # Eating occasion — comfort, light, quick, festive…
    #
    # A filter rather than a boost. "comfort food for a cold evening" is a
    # request for comfort food; ranking comfort higher while still returning
    # everything else meant Angel Food Cake (moods: light, indulgent) came
    # first, purely because its title contains the word "food".
    moods: list[str] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    # Dominant taste — sweet, umami, smoky… and coarse ingredient category —
    # vegetables, grains, fish… Both filters for the same reason as moods: a
    # user who picks "spicy" is excluding mild food, not ranking it lower.
    flavor_profiles: list[str] = field(default_factory=list)
    food_groups: list[str] = field(default_factory=list)
    convenience: list[str] = field(default_factory=list)
    nutrition_claims: list[str] = field(default_factory=list)
    nutri_scores: list[str] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)
    title_query: str | None = None
    # The raw question, used for ranking only — never filtering.
    #
    # Filters narrow the candidate set; text decides the order within it. Before
    # this, text was consulted *only* when nothing else was extracted, so
    # "dairy free dessert" applied the dairy_free filter and then returned the
    # matching set alphabetically — savoury rice dishes ahead of any dessert,
    # because the extractor drops dish nouns and nothing was left to rank by.
    rank_query: str | None = None
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
# canonicalization) mapped to the raw `source` keyword values in the index.
#
# Derived from the `catalog.sources` registry rather than written out here.
# These two dicts were hand-maintained and fell five sources behind it: the
# registry gained Slovenian Kitchen, Irish Heart Foundation, Best of Hungary,
# SuperValu and The Hungary Soul, the maps did not, and because an unmapped
# slug falls through to itself the filter became `source: ["slovenian_kitchen"]`
# against an index storing "Slovenian Kitchen". `source` is a keyword field, so
# that is an exact-match miss — the UI offered five filters that returned zero
# recipes and no error. Deriving both directions means adding a source to the
# registry is now the only step.
def _build_source_slug_to_raw() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for source in _REGISTERED_SOURCES:
        mapping[source.slug] = [source.raw]
        # Alternative spellings the filter contract has always accepted
        # ("safefood" for irish_safefood). `_norm` lowercases the incoming
        # value, so every key must be lowercase to be reachable.
        for alias in source.aliases:
            key = str(alias).strip().lower()
            if key:
                mapping.setdefault(key, [source.raw])
    return mapping


def _build_raw_source_to_slug() -> dict[str, str]:
    """Reverse direction, for folding facet buckets back onto slugs.

    Keyed on the lowercased raw value because the facet path lowercases the
    bucket key before looking it up.
    """
    return {
        str(source.raw).strip().lower(): source.slug
        for source in _REGISTERED_SOURCES
    }


_SOURCE_SLUG_TO_RAW: dict[str, list[str]] = _build_source_slug_to_raw()

_RAW_SOURCE_TO_SLUG: dict[str, str] = _build_raw_source_to_slug()

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


def _norm_tags(items: list[str]) -> list[str]:
    """Normalize to the index's tag spelling: lowercase, underscore-separated.

    The constraint extractor emits natural language ("gluten free") while the
    index stores slugs ("gluten_free"), and a `term` query does not analyse its
    input — so the mismatch silently matched zero documents rather than
    erroring. That quietly hid 3,043 gluten-free, 3,768 dairy-free and 5,119
    nut-free recipes from every dietary search.

    Hyphens are folded too, not just spaces: the extractor is not deterministic
    about the separator — the same question yielded "gluten free" on one run and
    "gluten-free" on the next, and only the first was being slugified.

    Applied only to tag fields. Title keywords and ingredient names must keep
    their spaces, which is why this is separate from `_norm`.
    """
    out: list[str] = []
    for item in items or ():
        # `None` must be skipped before str(): it would slug to the literal
        # tag "none", which matches nothing and empties the result set.
        if item is None:
            continue
        slug = "_".join(str(item).strip().lower().replace("-", " ").split())
        if slug and slug not in out:
            out.append(slug)
    return out


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


# index -> (is_nested, resolved_at). Only successful reads are stored, and they
# expire so an alias repoint is picked up without a restart.
_INGREDIENTS_NESTED: dict[str, tuple[bool, float]] = {}
_INGREDIENTS_NESTED_LOCK = threading.Lock()
_INGREDIENTS_NESTED_TTL = 300.0

# How deep title reranking reaches. Beyond this the pool would cost more to hold
# than the reordering is worth, and reordering the 200th-best match by title
# similarity changes nothing anyone will see.
_RERANK_POOL = 100


def _ingredients_is_nested(index: str) -> bool:
    """Whether ``ingredients`` is a nested field on this index.

    Read once per index rather than per query, but **only a successful read is
    cached**. A failed one used to be cached as `False` forever: one three-second
    timeout during startup permanently downgraded every ingredient and
    ingredient-derived allergen filter to the flat shape, which matches nothing
    against the nested catalog index. Silently — a flat clause is a valid query
    that returns no hits, so search kept answering 200 with an empty result.

    On failure the flat shape is still the answer *for that call*, because a
    nested clause errors against a flat index while a flat clause merely misses.
    The next call retries, so the mistake lasts one request rather than a
    process lifetime.

    Entries also expire, so repointing the alias at a differently-mapped index
    is picked up without a restart — an alias flip is exactly when this answer
    changes and exactly when nobody thinks to redeploy.
    """
    now = time.monotonic()
    cached = _INGREDIENTS_NESTED.get(index)
    if cached is not None and now - cached[1] < _INGREDIENTS_NESTED_TTL:
        return cached[0]
    with _INGREDIENTS_NESTED_LOCK:
        cached = _INGREDIENTS_NESTED.get(index)
        if cached is not None and now - cached[1] < _INGREDIENTS_NESTED_TTL:
            return cached[0]
        nested = False
        resolved = False
        try:
            settings = get_settings()
            # The full `_mapping`, not `_mapping/field/ingredients`: the field
            # API reports leaf fields only and returns an empty object for a
            # nested *parent*, so it cannot answer this question at all.
            resp = get_http_session().get(
                f"{settings.elastic_url}/{index}/_mapping",
                timeout=settings.elastic_timeout,
            )
            if resp.ok:
                for body in resp.json().values():
                    spec = (
                        body.get("mappings", {})
                        .get("properties", {})
                        .get("ingredients", {})
                    )
                    if spec.get("type") == "nested":
                        nested = True
                    resolved = True
                    break
        except Exception:  # noqa: BLE001
            logger.warning(
                "could not resolve ingredients mapping for %s — assuming flat "
                "for this request and retrying on the next one",
                index,
            )
        if resolved:
            _INGREDIENTS_NESTED[index] = (nested, now)
        return nested


def _ingredient_clause(name: str, *, nested: bool) -> dict[str, Any]:
    """Match one ingredient, in whichever shape the target index uses.

    ``recipes_v2`` stores ``ingredients`` as flat analysed text. The catalog
    index stores it as ``nested`` objects, with a flat ``ingredient_names``
    keyword copy for cheap terms filters.

    ``ingredient_names`` is deliberately NOT used here: it holds whole
    ingredient names, so a term query for "chicken" matches only the 73 recipes
    with an ingredient literally called "chicken" — not the 304 with "chicken
    breast" or the 108 with "chicken thigh". This clause drives allergen
    exclusion, so under-matching is a safety failure, not a relevance one.
    The analysed nested field is what matches the same 1,705 recipes the flat
    text field did.
    """
    if nested:
        return {
            "nested": {
                "path": "ingredients",
                "query": {"match_phrase": {"ingredients.name": name}},
            }
        }
    return {"match_phrase": {"ingredients": name}}


# Words that carry no dish information but appear in recipe titles, so an
# OR-matched ranking query scores on them. "comfort food for a cold evening"
# ranked *Angel Food Cake* first on the word "food" alone. Stripped from the
# ranking text only — filters and title searches are unaffected.
_RANK_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "any", "are", "can", "cook", "dish", "dishes", "eat",
        "evening", "food", "for", "from", "have", "how", "i", "idea", "ideas",
        "in", "is", "it", "make", "me", "meal", "meals", "my", "of", "on",
        "or", "please", "recipe", "recipes", "some", "something", "that",
        "the", "to", "want", "what", "with", "would",
    }
)


def _strip_rank_stopwords(question: str) -> str:
    """Drop uninformative words from a ranking query.

    Returns the original text if nothing meaningful survives — an empty
    ranking clause would silently disable relevance ordering entirely.
    """
    kept = [
        word
        for word in re.findall(r"[\w'-]+", question.lower())
        if word not in _RANK_STOPWORDS
    ]
    return " ".join(kept) if kept else question


def _rank_should_queries(question: str) -> list[dict[str, Any]]:
    """Ranking clauses for a free-text question.

    Deliberately OR-matched, unlike `_title_should_queries`. A question is a
    sentence and only some of its words name the dish: "dairy free dessert"
    has one word worth matching on. Requiring the whole phrase (operator="and",
    as every title clause does) scores every document zero and leaves the
    filtered set in alphabetical order — savoury rice ahead of any dessert.

    Boosts stay well below the title-search clauses so an explicit title query
    always dominates, and ingredients are weighted low so a passing mention
    never outranks the dish name.
    """
    return [
        {"match_phrase": {"title": {"query": question, "boost": 30}}},
        {"match": {"title": {"query": question, "operator": "or", "boost": 6}}},
        {"match": {"ingredients": {"query": question, "operator": "or", "boost": 2}}},
    ]


def build_es_query(c: RecipeSearchConstraints) -> dict[str, Any]:
    """Translate constraints into an Elasticsearch search body.

    Hard constraints go in `filter` context (no scoring, cached bitsets).
    Title keywords go in ``must`` as AND constraints.
    """
    filter_: list[dict] = []
    must: list[dict] = []
    must_not: list[dict] = []
    should: list[dict] = []
    region = _resolve_region(c.region)

    # Only profiled recipes.
    #
    # `has_profile`, not `exists: nutri_score_eu`. The EU score was a proxy that
    # held for the bulk-imported corpus, where every recipe was profiled against
    # the global composition pool — but a recipe created through the API is
    # profiled against the region its author chose. One created with region=IE
    # gets an Irish score and no EU one, so the proxy excluded it from browse
    # entirely: profiled in Postgres, profiled in its own document, and
    # invisible anyway.
    #
    # The two markers currently differ on exactly one document, so this changes
    # nothing about the existing corpus and stops the bug recurring for every
    # recipe a user creates.
    filter_.append({"term": {"has_profile": True}})

    # Soft-deleted recipes are hidden everywhere; the console opts in via
    # include_disabled to find and re-enable them.
    if not c.include_disabled:
        must_not.append(es_not_disabled_clause())

    # Included ingredients — every term must match (AND).
    ing_nested = _ingredients_is_nested(get_settings().elastic_index)
    for ing in _norm(c.include_ingredients):
        filter_.append(_ingredient_clause(ing, nested=ing_nested))

    # Excluded ingredients — none may match.
    for ing in _norm(c.exclude_ingredients):
        must_not.append(_ingredient_clause(ing, nested=ing_nested))

    # Allergens — exclude any recipe carrying one of them.
    allergens = _norm(c.exclude_allergens)
    if allergens:
        must_not.append({"terms": {"allergens": allergens}})

    # Supported consumer groups use the explicit, three-state composition
    # assessment. Other legacy dietary labels continue to use recipe tags.
    for tag in _norm_tags(c.diet_tags):
        if tag in _SUPPORTED_CONSUMER_GROUPS:
            # vegan/vegetarian: prefer the FATO three-state assessment, fall
            # back to the source tag.
            #
            # Filtering on `suitable_for` alone made every vegetarian and vegan
            # search return zero. The field is real and correct — it is simply
            # unpopulated, because classify_vegan_vegetarian.py has never been
            # run against this corpus (the graph holds zero ConsumerGroup
            # nodes), while `tags` carries vegetarian on 3,916 recipes and
            # vegan on 2,143. Matching either uses the evidence where it exists
            # and stays correct once the classifier is run.
            filter_.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"suitable_for": tag}},
                            {"term": {"diet_tags": tag}},
                            {"term": {"tags": tag}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        else:
            filter_.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"tags": tag}},
                            {"term": {"diet_tags": tag}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        # Whichever branch matched, verify the claim against the ingredients.
        # The comment above explains why the tag is trusted at all; this is why
        # it is not trusted alone. 44% of recipes tagged `vegan` here contain
        # meat, dairy, egg or honey — the tags arrived with the source data and
        # were never checked against the ingredient lists.
        filter_.extend(D.exclusion_filters([tag]))

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
        # Expand to the legacy spellings v2 holds, AND fold through the catalog
        # registry so meal-slot words resolve. "dinner" and "lunch" were literal
        # dish_type values in v2 but are canonicalized to "main-dish" in the
        # catalog index, so a filter for "dinner" would otherwise match nothing
        # after the read flip.
        expanded: list[str] = []
        for dt in dish_types:
            expanded.extend(_DISH_TYPE_VARIANTS.get(dt, [dt]))
            canonical = canonical_course_type(dt)
            if canonical:
                expanded.append(canonical)
        expanded = list(dict.fromkeys(expanded))
        # Match either field name. recipes_v2 stores `dish_types`; recipes_v3
        # stores `course_types` and no longer carries the duplicate. A `should`
        # with minimum_should_match keeps one filter working against either
        # index, so the read flip does not need this call site changed.
        filter_.append(
            {
                "bool": {
                    "should": [
                        {"terms": {"dish_types": expanded}},
                        {"terms": {"course_types": expanded}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    # Annotation facets. Absent on recipes_v2, so these are no-ops against the
    # old index and become active with the catalog index — one call site across
    # the read flip.
    # `_norm_tags`, not `_norm`: these vocabularies contain multi-word values
    # stored with underscores — `middle_eastern`, `herbs_and_spices`,
    # `nuts_and_seeds`. `terms` does not analyse its input, so a caller writing
    # "Middle Eastern" or "middle-eastern" would match zero documents and get a
    # successful empty response. The UI echoes facet keys back verbatim and is
    # unaffected; the tools endpoint and the question path are not.
    if c.moods:
        filter_.append({"terms": {"moods": _norm_tags(c.moods)}})
    if c.cuisines:
        filter_.append({"terms": {"cuisines": _norm_tags(c.cuisines)}})
    if c.flavor_profiles:
        filter_.append({"terms": {"flavor_profiles": _norm_tags(c.flavor_profiles)}})
    if c.food_groups:
        filter_.append({"terms": {"food_groups": _norm_tags(c.food_groups)}})
    if c.convenience:
        filter_.append({"terms": {"convenience": _norm_tags(c.convenience)}})
    if c.nutrition_claims:
        filter_.append({"terms": {"nutrition_claims": _norm_tags(c.nutrition_claims)}})
    if c.nutri_scores:
        grades = [
            str(value).strip().upper()
            for value in c.nutri_scores
            if str(value).strip().upper() in {"A", "B", "C", "D", "E"}
        ]
        if grades:
            filter_.append({"terms": {f"nutri_score_{region}": list(dict.fromkeys(grades))}})

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
    for tag in _norm_tags(c.boost_tags):
        should.append({"term": {"tags": {"value": tag, "boost": 1.5}}})

    title_query = str(c.title_query or "").strip()
    if title_query:
        should.extend(_title_should_queries(title_query))

    # Ranking-only text. Added when the caller supplied a question but the
    # extractor turned it into filters alone; contributes score without ever
    # constraining, so a filtered set still comes back in relevance order.
    rank_query = str(c.rank_query or "").strip()
    apply_rank_query = bool(rank_query) and not title_query
    if apply_rank_query:
        should.extend(_rank_should_queries(_strip_rank_stopwords(rank_query)))

    limit = max(1, min(int(c.limit), 100))
    offset = max(0, int(c.offset))
    query: dict[str, Any] = {"bool": {"filter": filter_, "must": must, "must_not": must_not}}
    if should:
        query["bool"]["should"] = should
        # Only a title_query makes text mandatory. rank_query must never
        # exclude a document that satisfies the filters.
        query["bool"]["minimum_should_match"] = 1 if title_query else 0

    if title_query or apply_rank_query:
        sort: list[Any] = [
            "_score",
            {"expert_recipe": "desc"},
            {"source_rank": "asc"},
            {"has_profile": "desc"},
            {"title.kw": "asc"},
            {"id": "asc"},
        ]
    elif should:
        # Personalization boosts must be able to reorder across source ranks,
        # otherwise _score (4th key) almost never breaks a tie. Experts stay
        # pinned first.
        sort = [
            {"expert_recipe": "desc"},
            "_score",
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
            # `dish_types` for recipes_v2, `course_types` for the catalog index.
            # Requesting an aggregation on a field the index lacks yields an
            # empty bucket list rather than an error, so both can be asked for
            # across the read flip and folded together in _collect_facets.
            "dish_types": {"terms": {"field": "dish_types", "size": 100}},
            "course_types": {"terms": {"field": "course_types", "size": 100}},
            "sources": {"terms": {"field": "source", "size": 50}},
            # Annotation facets. Absent on recipes_v2, populated on the catalog
            # index — this is what lets the UI offer cuisine, mood, flavour and
            # food-group filters with live counts.
            "cuisines": {"terms": {"field": "cuisines", "size": 40}},
            "moods": {"terms": {"field": "moods", "size": 20}},
            "flavor_profiles": {"terms": {"field": "flavor_profiles", "size": 20}},
            "food_groups": {"terms": {"field": "food_groups", "size": 20}},
            "diet_tags": {"terms": {"field": "diet_tags", "size": 40}},
            "convenience": {"terms": {"field": "convenience", "size": 10}},
            "nutrition_claims": {"terms": {"field": "nutrition_claims", "size": 20}},
            "nutri_scores": {"terms": {"field": f"nutri_score_{region}", "size": 5}},
            "allergens": {"terms": {"field": "allergens", "size": 30}},
        }

    return body


def _canonical_course_types(values: object) -> list[str]:
    """Fold indexed course/dish-type variants onto their canonical spelling."""
    if not isinstance(values, (list, tuple)):
        values = [values] if values else []
    out: list[str] = []
    for value in values:
        key = str(value or "").strip().lower()
        canonical = _DISH_TYPE_CANONICAL.get(key, key)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def _hit_to_card(hit: dict, region: str) -> dict[str, Any]:
    src = hit.get("_source", {})
    return {
        # v2 put the business id in `id`; the catalog index puts a UUID there
        # and the business id in `recipe_id`. Prefer the explicit field.
        "recipe_id": src.get("recipe_id") or src.get("id"),
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
        "allergens": src.get("allergens") or [],
        "allergen_evidence": src.get("allergen_evidence") or [],
        "suitable_for": src.get("suitable_for") or [],
        "consumer_suitability": src.get("consumer_suitability") or [],
        # Folded to one spelling before it leaves the API. The index holds both
        # main-dish/main_dish, desserts/dessert and snacks/snack because two
        # builders wrote it from two different owners; clients should never have
        # to know that.
        "course_types": _canonical_course_types(
            src.get("course_types") or src.get("dish_types") or []
        ),
        # Carried on the card so a result can show what it was matched on.
        # Empty lists for any recipe the annotation pass has not reached, which
        # the client renders as no chips rather than as missing data.
        "cuisines": src.get("cuisines") or [],
        "moods": src.get("moods") or [],
        "flavor_profiles": src.get("flavor_profiles") or [],
        "food_groups": src.get("food_groups") or [],
        "convenience": src.get("convenience") or [],
        "nutrition_claims": src.get("nutrition_claims") or [],
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

    # The rerank pool. Title reranking fixes the *top* of the list — it stops a
    # longer fuzzy match outranking the intended title — so it needs the whole
    # candidate set in memory, which is only affordable for the first page or
    # two.
    #
    # Past that the pool is abandoned and ordinary ES paging takes over. The old
    # code kept the pool, capped it at 100, and then sliced `[offset:offset+n]`
    # out of it: any offset at or beyond 100 sliced past the end and returned an
    # empty page alongside a non-zero total. The lexical fallback sets a
    # title_query for every bare-noun search, so that was every deep page of a
    # plain text query — paging "pasta" simply stopped working at page 9.
    rerank = bool(title_query) and (result_offset + result_limit) <= _RERANK_POOL
    if rerank:
        body["from"] = 0
        body["size"] = min(_RERANK_POOL, max(50, result_offset + result_limit))

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
    result_hits = hits.get("hits", [])
    if rerank:
        result_hits = _rerank_title_hits(result_hits, title_query)
        result_hits = result_hits[result_offset : result_offset + result_limit]
    region = _resolve_region(c.region)
    out = {
        "results": [_hit_to_card(h, region) for h in result_hits],
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

    # dish_types (recipes_v2) and course_types (catalog index) are the same
    # concept under two names; fold both into one bucket so the UI sees a
    # single facet whichever index is behind the alias.
    dish_map: dict[str, int] = {}
    for agg_name in ("dish_types", "course_types"):
        for b in aggregations.get(agg_name, {}).get("buckets", []):
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

    # Annotation facets, passed through under their own names. Values are
    # already canonical in the index (the annotation pipeline validates against
    # a closed vocabulary), so no folding is needed — a facet key can be sent
    # straight back as a filter value.
    #
    # A facet with no buckets is omitted rather than emitted empty: the UI
    # renders a section per facet it receives, and an always-present empty
    # "Cuisine" panel reads as broken. Omitting it means the section simply
    # does not appear until the corpus has the data.
    for agg_name in ("cuisines", "moods", "flavor_profiles", "food_groups",
                     "diet_tags", "allergens"):
        bucket_map: dict[str, int] = {}
        for b in aggregations.get(agg_name, {}).get("buckets", []):
            key = str(b.get("key", "")).strip().lower()
            if not key:
                continue
            bucket_map[key] = bucket_map.get(key, 0) + int(b.get("doc_count", 0))
        if bucket_map:
            facets[agg_name] = bucket_map

    return facets
