"""The single registry of recipe sources.

Before this module the same source mapping was expressed in at least four
places, in two languages:

- ``_SOURCE_SLUG_TO_RAW`` / ``_RAW_SOURCE_TO_SLUG`` in ``tools/es_recipe_search``
- ``_CURATED_SOURCES`` in ``utils/es_recipe_projection``
- ``_TRUSTED_SOURCES`` in ``repositories/neo4j_recipes``
- the ``if source_text == ...`` ladder resolving collection URNs in
  ``utils/nutrition_postgres.upsert_recipe_profiling_trace``
- ``_source_ground_truth_nutrition_source`` in ``api/routers/recipes``

Each carried its own spelling of the same seven sources, and adding a source
meant finding all of them. This module is the one place that knows.

Dish types get the same treatment. The corpus carries spelling variants
(``main-dish``/``main_dish``, ``desserts``/``dessert``, ``snacks``/``snack``)
because two different writers built the index from two different owners — the
offline builder read ``Recipe.meal_type``/``Recipe.dish_type`` properties while
the runtime projection read ``Tag`` nodes with ``category='dish-type'``. Query
code has been expanding filters across variants and folding facet buckets back
together ever since. Canonicalization belongs at index time, so
``canonical_course_type`` is applied on the way in and the query layer stops
compensating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Source:
    """One recipe source and everything the system needs to know about it."""

    slug: str
    """Canonical identifier — the API/UI filter contract."""

    raw: str
    """The literal ``source`` value stored on Neo4j ``:Recipe`` and in ES."""

    display_name: str

    aliases: tuple[str, ...] = ()
    """Additional accepted spellings when resolving user/API input."""

    collection_urn: str | None = None
    """``urn:rcollection:*`` in the wisefood-data-api catalog, if one exists."""

    curated: bool = False
    """Human-curated content — ranked above scraped corpora."""

    trusted: bool = False
    """High enough quality to boost in random sampling."""

    ground_truth_nutrition_sources: tuple[str, ...] = ()
    """Nutrition sources shipped *with* the recipes, preferred over pipeline
    output. Ordered most-authoritative first."""

    retired: bool = False
    """No longer part of the corpus. Retained so historical profile rows and
    collection references still resolve."""


SOURCES: tuple[Source, ...] = (
    Source(
        slug="healthyfoods",
        raw="HealthyFoods",
        display_name="Healthy Food Guide",
        collection_urn="urn:rcollection:healthyfood",
        curated=True,
        trusted=True,
        ground_truth_nutrition_sources=("healthyfoods_original", "healthyfoods"),
    ),
    Source(
        slug="foodhero",
        raw="FoodHero",
        display_name="Food Hero",
        collection_urn="urn:rcollection:foodhero",
        curated=True,
        trusted=True,
    ),
    Source(
        slug="myplate",
        raw="MyPlate",
        display_name="MyPlate",
        # NOTE: nutrition_postgres resolves this to "urn:rcollection:myplate",
        # but no such document exists in the catalog's rcollections index (only
        # recipe1m, healthyfood, foodhero and rcsi-recipes do). Left as None
        # rather than propagating a dangling reference; create the collection
        # and fill this in.
        collection_urn=None,
        ground_truth_nutrition_sources=("myplate",),
    ),
    Source(
        slug="irish_safefood",
        raw="Curated Irish Recipes",
        display_name="safefood (RCSI)",
        aliases=("safefood", "irish safefood", "irish-safefood"),
        collection_urn="urn:rcollection:rcsi-recipes",
        curated=True,
        trusted=True,
        ground_truth_nutrition_sources=("safefood_rcsi", "safefood_web", "safefood"),
    ),
    Source(
        slug="hungarian",
        raw="Curated Hungarian Recipes",
        display_name="Curated Hungarian Recipes",
        curated=True,
        ground_truth_nutrition_sources=("planeat",),
    ),
    Source(
        slug="slovenian",
        raw="Curated Slovenian Recipes",
        display_name="Curated Slovenian Recipes",
        curated=True,
        ground_truth_nutrition_sources=("slovenian_original",),
    ),
    # Web sources added after the original six-source registry was introduced.
    # Keep them here even though they do not yet have catalog collection URNs:
    # the full-corpus builder filters Neo4j by ``active_sources()``, so omitting
    # one here makes an otherwise successful rebuild silently delete that
    # source from Elasticsearch.
    Source(
        slug="best_of_hungary",
        raw="Best of Hungary",
        display_name="Best of Hungary",
    ),
    Source(
        slug="irish_heart_foundation",
        raw="Irish Heart Foundation",
        display_name="Irish Heart Foundation",
    ),
    Source(
        slug="slovenian_kitchen",
        raw="Slovenian Kitchen",
        display_name="Slovenian Kitchen",
    ),
    Source(
        slug="supervalu",
        raw="SuperValu",
        display_name="SuperValu",
    ),
    Source(
        slug="the_hungary_soul",
        raw="The Hungary Soul",
        display_name="The Hungary Soul",
    ),
    Source(
        slug="user",
        raw="user",
        display_name="User-created",
    ),
    Source(
        slug="recipe1m",
        raw="recipe1m",
        display_name="Recipe1M+",
        retired=True,
    ),
)


_BY_SLUG: dict[str, Source] = {s.slug: s for s in SOURCES}
_BY_RAW: dict[str, Source] = {s.raw.strip().lower(): s for s in SOURCES}
_BY_ANY: dict[str, Source] = {}
for _s in SOURCES:
    for _key in (_s.slug, _s.raw, _s.display_name, *_s.aliases):
        _BY_ANY.setdefault(str(_key).strip().lower(), _s)


def resolve(value: object) -> Source | None:
    """Resolve a slug, alias, raw value or display name to its Source."""
    return _BY_ANY.get(str(value or "").strip().lower())


def by_slug(slug: object) -> Source | None:
    return _BY_SLUG.get(str(slug or "").strip().lower())


def by_raw(raw: object) -> Source | None:
    return _BY_RAW.get(str(raw or "").strip().lower())


def slug_for(value: object) -> str | None:
    source = resolve(value)
    return source.slug if source else None


def raw_for(value: object) -> str | None:
    source = resolve(value)
    return source.raw if source else None


def collection_urn_for(value: object) -> str | None:
    source = resolve(value)
    return source.collection_urn if source else None


def ground_truth_nutrition_sources(value: object) -> tuple[str, ...]:
    source = resolve(value)
    return source.ground_truth_nutrition_sources if source else ()


def active_sources() -> tuple[Source, ...]:
    return tuple(s for s in SOURCES if not s.retired)


def curated_slugs() -> frozenset[str]:
    return frozenset(s.slug for s in SOURCES if s.curated)


def trusted_slugs() -> frozenset[str]:
    return frozenset(s.slug for s in SOURCES if s.trusted)


def source_rank(value: object) -> int:
    """Ordering key for result ranking — lower sorts first.

    Curated-and-trusted beats curated beats everything else, and a retired
    source sorts last so stragglers never outrank live content.
    """
    source = resolve(value)
    if source is None:
        return 99
    if source.retired:
        return 90
    if source.curated and source.trusted:
        return 0
    if source.curated:
        return 1
    return 10


# --------------------------------------------------------------------------- #
# Course types (called "dish types" in the v2 index)
# --------------------------------------------------------------------------- #

COURSE_TYPES: tuple[str, ...] = (
    "breakfast",
    "main-dish",
    "starter",
    "side",
    "salad",
    "soup",
    "desserts",
    "snacks",
    "beverages",
)

_COURSE_TYPE_VARIANTS: dict[str, tuple[str, ...]] = {
    "main-dish": ("main-dish", "main_dish", "main dish", "lunch", "dinner"),
    "desserts": ("desserts", "dessert"),
    "snacks": ("snacks", "snack"),
    "beverages": ("beverages", "beverage", "drinks"),
    "breakfast": ("breakfast",),
    "salad": ("salad", "salads"),
    "soup": ("soup", "soups"),
    "starter": ("starter", "starters", "appetizer", "appetizers"),
    "side": ("side", "sides", "side-dish", "side_dish"),
}

_COURSE_TYPE_CANONICAL: dict[str, str] = {
    variant: canonical
    for canonical, variants in _COURSE_TYPE_VARIANTS.items()
    for variant in variants
}


def canonical_course_type(value: object) -> str | None:
    """Fold a course/dish-type spelling onto its canonical form.

    ``lunch`` and ``dinner`` collapse into ``main-dish``: they are meal slots,
    not courses, and the corpus only ever used them as the latter.
    """
    return _COURSE_TYPE_CANONICAL.get(str(value or "").strip().lower())


def canonical_course_types(values: Iterable[object]) -> list[str]:
    """Canonicalize a collection, dropping unknowns and preserving order."""
    out: list[str] = []
    for value in values or ():
        canonical = canonical_course_type(value)
        if canonical and canonical not in out:
            out.append(canonical)
    return out
