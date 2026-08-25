"""The catalog's concrete entities: ``RECIPE`` and ``PROFILE``.

Both derive their denormalized fields in ``validate()``, which every write path
goes through — single writes and bulk backfills alike. That is what stops the
two writers drifting apart the way the offline builder and the runtime
projection did: there is now one place that knows how to build a recipe
document, and it is impossible to write one without going through it.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog.entity import DependentEntity, Entity, ValidationError
from recipe_wrangler.utils.convenience import compute_convenience_tags

# Nutri-Score labels in quality order; the rank is what sorting uses.
NUTRI_RANKS: dict[str, int] = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}

# Region precedence when falling back to the flat per-region score fields. EU
# first because it is the only region computed for the whole corpus; the rest
# are a deterministic tail so two documents never resolve differently.
DEFAULT_SCORE_REGION_ORDER: tuple[str, ...] = ("eu", "ie", "hu", "slovenian")


def nutri_label(value: object) -> str | None:
    """Normalize ``Nutriscore_A`` / ``a`` / ``A`` to ``A``."""
    text = str(value or "").strip()
    if not text:
        return None
    letter = text.rsplit("_", 1)[-1].upper()
    return letter if letter in NUTRI_RANKS else None


def nutri_rank(value: object) -> int | None:
    label = nutri_label(value)
    return NUTRI_RANKS.get(label) if label else None


def normalize_title(value: object) -> str:
    """Fold a title for exact matching without discarding non-Latin letters."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").strip()).casefold()
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in stripped).split())


class Recipe(Entity):
    def __init__(self, alias: str | None = None, *, register: bool = True):
        super().__init__(
            name="recipe",
            alias=alias or get_settings().catalog_recipes_alias,
            register=register,
        )

    def validate(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)

        if not str(doc.get("title") or "").strip():
            raise ValidationError(f"recipe {doc.get('urn')} has no title")

        doc.setdefault("recipe_id", self.local_id(doc["urn"]))
        doc["title_normalized"] = normalize_title(doc.get("title"))

        # --- source-derived fields, all from the one registry -------------- #
        source = S.resolve(doc.get("source"))
        if source is not None:
            doc["source"] = source.raw
            doc["source_name"] = source.display_name
            doc["source_rank"] = S.source_rank(source.slug)
            if source.collection_urn:
                doc["collection_urn"] = source.collection_urn
        else:
            doc["source_rank"] = S.source_rank(doc.get("source"))

        # --- course types normalized at index time ------------------------- #
        # Both legacy spellings and the meal-slot values collapse here, so the
        # query layer no longer expands filters across variants and fold facet
        # buckets back together.
        raw_courses: list[Any] = []
        for key in ("course_types", "dish_types"):
            value = doc.get(key)
            if isinstance(value, (list, tuple)):
                raw_courses.extend(value)
            elif value:
                raw_courses.append(value)
        # `course_types` is the only name. `dish_types` was the v2 spelling and
        # was mirrored here during the transition, but the two held byte-identical
        # values — one concept, two fields, which is how the corpus ended up with
        # main-dish and main_dish as separate buckets in the first place.
        # "Dish type" is also ambiguous with *dish family* (pasta, curry), which
        # is a genuinely different facet.
        # Course labels are Elasticsearch-owned annotations.  Canonicalize
        # their spelling, but never second-guess a stored model/human value
        # from the title: doing so discarded paid annotations during the v4
        # rebuild (notably recipes whose titles end in "sauce" or "marinade").
        courses = S.canonical_course_types(raw_courses)
        doc.pop("dish_types", None)
        if courses:
            doc["course_types"] = courses
        else:
            doc.pop("course_types", None)

        # --- ingredients --------------------------------------------------- #
        ingredients = doc.get("ingredients")
        if isinstance(ingredients, list):
            names, cleaned = [], []
            for position, item in enumerate(ingredients):
                if isinstance(item, dict):
                    entry = dict(item)
                    entry.setdefault("position", position)
                    name = str(entry.get("name") or "").strip()
                elif item:
                    name = str(item).strip()
                    entry = {"name": name, "position": position}
                else:
                    continue
                if not name:
                    continue
                entry["name"] = name
                cleaned.append(entry)
                lowered = name.lower()
                if lowered not in names:
                    names.append(lowered)
            doc["ingredients"] = cleaned
            doc["ingredient_names"] = names
            doc["ingredient_count"] = len(cleaned)

        doc["has_image"] = bool(str(doc.get("image_url") or "").strip())

        # --- deterministic v4 fields -------------------------------------- #
        # These are derived on every write, so a create, patch and full corpus
        # rebuild cannot disagree or preserve stale values from Elasticsearch.
        doc["convenience"] = compute_convenience_tags(
            doc.get("duration"), doc.get("ingredient_count")
        )
        doc["schema_version"] = 4

        # --- the single score a list and a detail page must agree on -------- #
        self._apply_default_score(doc)

        # --- planning eligibility ------------------------------------------- #
        self._apply_planning_tier(doc)

        return doc

    @staticmethod
    def _apply_planning_tier(doc: dict[str, Any]) -> None:
        """Decide whether a planner may serve this recipe unattended.

        An explicit ``excluded`` is never overridden — that is a human decision
        and re-deriving it on every write would silently undo it. Otherwise the
        tier is derived, so a recipe that gains a profile is promoted on its
        next write without anyone remembering to.

        The bar for ``preferred`` is deliberately about *completeness*, not
        quality: a plan built from recipes with no nutrition data cannot honour
        a nutritional request, however good the cooking.
        """
        if doc.get("planning_tier") == "excluded":
            return

        source = S.resolve(doc.get("source"))
        disqualifiers: list[str] = []

        if str(doc.get("status") or "active") != "active":
            disqualifiers.append("not_active")
        if not str(doc.get("title") or "").strip():
            disqualifiers.append("no_title")
        if not doc.get("ingredients"):
            disqualifiers.append("no_ingredients")

        if disqualifiers:
            doc["planning_tier"] = "excluded"
            doc["planning_excluded_reason"] = disqualifiers[0]
            return

        doc.pop("planning_excluded_reason", None)
        well_formed = bool(
            doc.get("has_profile")
            and doc.get("default_nutri_score")
            and doc.get("course_types")
            and doc.get("serves")
        )
        doc["planning_tier"] = (
            "preferred" if well_formed and source and source.curated else "standard"
        )

    @staticmethod
    def _apply_default_score(doc: dict[str, Any]) -> None:
        """Choose one authoritative Nutri-Score for the document.

        A recipe showing C in a list and A on its detail page is what happens
        when each view picks its own region. The rule is fixed and recorded
        here: a score shipped by the source outranks anything the pipeline
        derived, then EU, then the best available region.
        """
        profiles = doc.get("profiles")
        if isinstance(profiles, list):
            ground_truth = [
                p for p in profiles if isinstance(p, dict) and p.get("is_ground_truth")
            ]
            for candidate in (ground_truth, profiles):
                for entry in candidate:
                    if not isinstance(entry, dict):
                        continue
                    label = nutri_label(entry.get("nutri_score"))
                    if label and (candidate is ground_truth or entry.get("region") == "eu"):
                        doc["default_nutri_score"] = label
                        doc["default_nutri_rank"] = NUTRI_RANKS[label]
                        return

        for region in DEFAULT_SCORE_REGION_ORDER:
            label = nutri_label(doc.get(f"nutri_score_{region}"))
            if label:
                doc["default_nutri_score"] = label
                doc["default_nutri_rank"] = NUTRI_RANKS[label]
                return

        existing = nutri_label(doc.get("default_nutri_score"))
        if existing:
            doc["default_nutri_score"] = existing
            doc["default_nutri_rank"] = NUTRI_RANKS[existing]
        else:
            doc.pop("default_nutri_score", None)
            doc.pop("default_nutri_rank", None)

    # ------------------------------------------------------------------ #
    def browse(
        self,
        *,
        course_type: str | None = None,
        cuisine: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = 24,
        offset: int = 0,
    ) -> dict[str, Any]:
        """The query behind Browse-by-Category.

        Filter values are canonicalized on the way in, so a UI asking for
        ``dessert`` and an index holding ``desserts`` still agree.
        """
        filters: list[dict[str, Any]] = []
        if course_type:
            canonical = S.canonical_course_type(course_type)
            if canonical is None:
                raise ValidationError(f"unknown course type {course_type!r}")
            filters.append({"term": {"course_types": canonical}})
        if cuisine:
            filters.append({"term": {"cuisines": str(cuisine).strip().lower()}})
        if source:
            resolved = S.resolve(source)
            filters.append({"term": {"source": resolved.raw if resolved else source}})

        return self.search(
            q=q,
            filters=filters,
            limit=limit,
            offset=offset,
            facets=["course_types", "cuisines", "source", "diet_tags", "food_groups"],
            sort=None if q else [{"source_rank": "asc"}, {"title.kw": "asc"}],
        )


class RecipeProfile(DependentEntity):
    def __init__(self, alias: str | None = None, *, register: bool = True):
        super().__init__(
            name="recipe_profile",
            alias=alias or get_settings().catalog_profiles_alias,
            parent_field="recipe_urn",
            parent_type="recipe",
            register=register,
        )

    def urn_for(self, recipe_id: str, nutrition_source: str) -> str:
        return self.compose_urn(str(recipe_id), str(nutrition_source))

    def validate(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc = dict(doc)
        recipe_id = str(doc.get("recipe_id") or "").strip()
        nutrition_source = str(doc.get("nutrition_source") or "").strip()
        if not recipe_id or not nutrition_source:
            raise ValidationError(
                "profile requires both recipe_id and nutrition_source"
            )
        doc["recipe_urn"] = self.parent_urn_for(recipe_id)

        score = doc.get("nutri_score")
        if isinstance(score, dict):
            label = nutri_label(score.get("label") or score.get("nutri_score"))
            if label:
                score = dict(score)
                score["label"] = label
                score["rank"] = NUTRI_RANKS[label]
                doc["nutri_score"] = score

        source = S.resolve(doc.get("source"))
        if source is not None:
            doc["source"] = source.raw
            if source.collection_urn:
                doc["collection_urn"] = source.collection_urn
            doc["is_ground_truth"] = (
                nutrition_source in source.ground_truth_nutrition_sources
            )
        return doc

    def for_recipe(self, recipe_id: str) -> list[dict[str, Any]]:
        return self.list_for_parent(self.parent_urn_for(str(recipe_id)))

    def orphans(self, known_recipe_urns: set[str]) -> Iterable[dict[str, Any]]:
        """Yield profiles whose parent recipe no longer exists."""
        for hit in self.scroll_all(source=["recipe_urn", "recipe_id", "source"]):
            if hit["_source"].get("recipe_urn") not in known_recipe_urns:
                yield {"_id": hit["_id"], **hit["_source"]}


_INSTANCES: dict[str, Entity] = {}


def recipe_entity() -> Recipe:
    """The shared ``Recipe`` entity.

    Built on first use rather than at import. A module-level ``RECIPE = Recipe()``
    would call ``get_settings()`` during import, so merely importing this module
    would require NEO4J_URI to be set and would fail test collection — the same
    import-time coupling that makes data-api's ``from main import config``
    fragile.
    """
    entity = _INSTANCES.get("recipe")
    if entity is None:
        entity = _INSTANCES["recipe"] = Recipe()
    return entity  # type: ignore[return-value]


def profile_entity() -> RecipeProfile:
    """The shared ``RecipeProfile`` entity, built on first use."""
    entity = _INSTANCES.get("recipe_profile")
    if entity is None:
        entity = _INSTANCES["recipe_profile"] = RecipeProfile()
    return entity  # type: ignore[return-value]


def __getattr__(name: str):
    """Allow ``from ...entities import RECIPE`` while staying lazy."""
    if name == "RECIPE":
        return recipe_entity()
    if name == "PROFILE":
        return profile_entity()
    raise AttributeError(name)
