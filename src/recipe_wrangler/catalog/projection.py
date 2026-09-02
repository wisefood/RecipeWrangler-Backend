"""Project one recipe from its owners into the catalog index.

Replaces ``utils/es_recipe_projection.project_recipe_to_es_v2``, which built a
``recipes_v2``-shaped document — flat ``ingredients``, no ``urn``, the recipe id
in ``id`` — and was rejected outright by the catalog index's strict mapping:

    400 document_parsing_exception
    object mapping for [ingredients] tried to parse field [null] as object

Because that projection was best-effort by contract it logged a warning and
returned ``False``, so **every recipe created or edited after the read flip was
invisible to search** and nothing said so.

Two changes follow from that:

1. The document is built by ``Recipe.validate`` — the same code the corpus
   rebuild uses. There is now one definition of a recipe document instead of
   two that had to be kept byte-compatible by hand.
2. Failure raises. A silent write failure is worse than a loud one: it is
   indistinguishable from success until someone notices the corpus is stale.
   Callers that genuinely cannot fail the request should catch
   ``ProjectionError`` and enqueue a retry, not swallow it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog.entities import nutri_label, recipe_entity
from recipe_wrangler.catalog.nutrition import apply_profiles, load_profiles_for
from recipe_wrangler.catalog.integrity import content_digest
from recipe_wrangler.utils.consumer_suitability import (
    SUITABILITY_CLASSIFICATION_VERSION,
)
from recipe_wrangler.utils.diet_tags import DIET_TAG_NAMES
from recipe_wrangler.utils.nutrition_claims import NUTRITION_CLAIM_TAG_NAMES
from recipe_wrangler.utils.es_recipe_evidence import (
    normalize_allergen_evidence,
    normalize_consumer_suitability,
    suitable_groups,
)
from recipe_wrangler.utils.neo4j_utils import driver

logger = logging.getLogger(__name__)


class ProjectionError(RuntimeError):
    """A recipe could not be projected into the catalog index."""


# Single-recipe form of the corpus query in scripts/catalog/build_recipes.py.
# Text properties are returned raw, not toString()'d: `instructions` is a
# StringArray of steps on most sources and a plain string on others, and
# toString() throws on an array rather than coercing it.
RECIPE_QUERY = """
MATCH (r:Recipe)
WHERE r.recipe_id = $rid OR r.id = $rid
CALL (r) {
  OPTIONAL MATCH (r)-[rel:HAS_INGREDIENT]->(i:Ingredient)
  WITH i, rel ORDER BY coalesce(rel.position, 2147483647), i.name
  RETURN collect(CASE WHEN i IS NULL THEN NULL ELSE {
    name: i.name,
    quantity: coalesce(rel.quantity, rel.measurement),
    unit: rel.unit,
    measurement: rel.measurement,
    position: rel.position,
    canonical_id: i.canonical_id
  } END) AS ingredients
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
  RETURN collect(DISTINCT al.name) AS allergens
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_CLASS]->(:FoodOnClass)
                 -[:SUBCLASS_OF*0..5]->(anc:FoodOnClass)
  RETURN collect(DISTINCT anc.foodon_id) AS ingredient_class_ancestors
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
                 -[:HAS_DECLARATION]->(d:AllergenDeclaration)
                 -[:CONCERNS]->(al:Allergen)
  RETURN collect(DISTINCT CASE WHEN al IS NULL THEN NULL ELSE {
    allergen: al.name, ingredient: i.name, ingredient_id: i.canonical_id,
    declaration_id: d.declaration_id, presence: d.presence,
    evidence_status: d.evidence_status, sources: d.sources,
    foodon_ids: d.foodon_ids, keyword_matches: d.keyword_matches,
    classification_version: d.classification_version
  } END) AS allergen_evidence
}
CALL (r) {
  OPTIONAL MATCH (r)-[s:SUITABILITY_FOR]->(g:ConsumerGroup)
  WHERE g.name IN ["vegan", "vegetarian"]
    AND s.classification_version = $suitability_version
  RETURN collect(DISTINCT CASE WHEN g IS NULL THEN NULL ELSE {
    group: g.name, status: s.status,
    blocking_ingredients: s.blocking_ingredients,
    reason_codes: s.reason_codes, sources: s.sources,
    classification_version: s.classification_version
  } END) AS consumer_suitability
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
  RETURN collect(DISTINCT t.name) AS tags,
         collect(DISTINCT CASE WHEN t.category = 'dish-type' THEN t.name END) AS tag_dish_types,
         collect(DISTINCT CASE WHEN t.category IN ['dietary','dietary_option']
                               AND t.name IN $diet_tag_names THEN t.name END) AS diet_tags,
         collect(DISTINCT CASE WHEN t.category = 'nutrition_claim'
                               AND t.name IN $nutrition_claim_names THEN t.name END) AS nutrition_claims
}
RETURN
  coalesce(r.recipe_id, r.id) AS recipe_id,
  r.title AS title, r.description AS description, r.instructions AS instructions,
  r.url AS url, r.image_url AS image_url, r.source AS source,
  r.source_id AS source_id,
  coalesce(r.duration_minutes, r.duration) AS duration,
  r.serves AS serves, r.cost_category AS cost_category,
  r.cost_category_code AS cost_category_code,
  r.cost_category_status AS cost_category_status,
  r.cost_price_coverage AS cost_price_coverage,
  coalesce(r.expert_recipe, false) AS expert_recipe,
  coalesce(r.status, "active") AS status,
  toString(properties(r)['disabled_at']) AS disabled_at,
  coalesce(r.has_profile, false) AS has_profile,
  r.creator AS creator,
  r.meal_type AS meal_type, r.dish_type AS dish_type, r.seasonality AS seasonality,
  ingredients, allergens, ingredient_class_ancestors,
  allergen_evidence, consumer_suitability, tags, tag_dish_types, diet_tags,
  nutrition_claims
LIMIT 1
"""


def _clean(value: object) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text in {"null", "None"} else text


def _clean_text(value: object) -> str:
    """Flatten a property that may be a string or a list of steps."""
    if isinstance(value, (list, tuple)):
        return "\n".join(p for p in (_clean(x) for x in value) if p)
    return _clean(value)


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out, seen = [], set()
    for value in values:
        item = _clean(value).lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _clean_ingredients(values: object) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    ingredients: list[dict[str, Any]] = []
    for fallback_position, value in enumerate(values):
        if not isinstance(value, dict):
            name = _clean(value)
            if name:
                ingredients.append({"name": name, "position": fallback_position})
            continue
        name = _clean(value.get("name"))
        if not name:
            continue
        position = _float(value.get("position"))
        entry: dict[str, Any] = {
            "name": name,
            "position": int(position if position is not None else fallback_position),
        }
        quantity = _float(value.get("quantity"))
        if quantity is not None:
            entry["quantity"] = quantity
        unit = _clean(value.get("unit"))
        if unit:
            entry["unit"] = unit
        measurement = _clean(value.get("measurement"))
        if measurement:
            entry["measurement"] = measurement
        canonical_id = _clean(value.get("canonical_id"))
        if canonical_id:
            entry["canonical_urn"] = f"urn:ingredient:{canonical_id}"
        ingredients.append(entry)
    return ingredients


def fetch_owner_row(recipe_id: str) -> dict[str, Any] | None:
    with driver.session() as session:
        rows = session.run(
            RECIPE_QUERY,
            {
                "rid": recipe_id,
                "suitability_version": SUITABILITY_CLASSIFICATION_VERSION,
                "diet_tag_names": list(DIET_TAG_NAMES),
                "nutrition_claim_names": list(NUTRITION_CLAIM_TAG_NAMES),
            },
        ).data()
    return rows[0] if rows else None


def build_document(
    row: dict[str, Any],
    *,
    profiles: list[dict[str, Any]] | None = None,
    preserve: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a catalog document from one owner row.

    ``preserve`` carries the Elasticsearch-only fields — annotations, their
    provenance, planning overrides — which no owner can reproduce. Without it a
    patch to a recipe's title would silently wipe its cuisine and mood.
    """
    source = S.resolve(row.get("source"))
    if source is not None and source.retired:
        raise ProjectionError(f"retired recipe source cannot be projected: {source.slug}")

    recipe_id = _clean(row.get("recipe_id"))

    consumer = normalize_consumer_suitability(
        row.get("consumer_suitability"),
        classification_version=SUITABILITY_CLASSIFICATION_VERSION,
    )

    doc: dict[str, Any] = {
        "urn": f"urn:recipe:{recipe_id}",
        "recipe_id": recipe_id,
        "title": _clean_text(row.get("title")),
        "description": _clean_text(row.get("description")) or None,
        "instructions": _clean_text(row.get("instructions")) or None,
        "url": _clean(row.get("url")) or None,
        "image_url": _clean(row.get("image_url")) or None,
        "source": _clean(row.get("source")),
        "source_id": _clean(row.get("source_id")) or None,
        "duration": _float(row.get("duration")),
        "serves": _float(row.get("serves")),
        "cost_category": _clean(row.get("cost_category")) or None,
        "cost_category_code": _int(row.get("cost_category_code")),
        "cost_category_status": _clean(row.get("cost_category_status")) or None,
        "cost_price_coverage": _float(row.get("cost_price_coverage")),
        "expert_recipe": bool(row.get("expert_recipe")),
        "status": _clean(row.get("status")) or "active",
        # Read from Neo4j *and* listed in ES_OWNED_FIELDS. Not a contradiction:
        # `preserve` wins for a document that already has one, so an existing
        # attribution is never overwritten, while a newly created recipe — which
        # has no document to preserve from — gets the creator the write recorded.
        # Without this the field was written to Neo4j on create and then dropped
        # by the very next step, so no recipe ever had a visible author.
        "creator": _clean(row.get("creator")) or None,
        "disabled_at": _clean(row.get("disabled_at")) or None,
        "has_profile": bool(profiles) or bool(row.get("has_profile")),
        "ingredients": _clean_ingredients(row.get("ingredients")),
        "ingredient_class_ancestors": _clean_list(row.get("ingredient_class_ancestors")),
        "allergens": _clean_list(row.get("allergens")),
        "allergen_evidence": normalize_allergen_evidence(row.get("allergen_evidence")),
        "consumer_suitability": consumer,
        "suitable_for": suitable_groups(consumer),
        "tags": _clean_list(row.get("tags")),
        "diet_tags": _clean_list(row.get("diet_tags")),
        "nutrition_claims": _clean_list(row.get("nutrition_claims")),
        "seasonality": _clean_list(row.get("seasonality")),
    }
    apply_profiles(doc, profiles or [])

    doc = {k: v for k, v in doc.items() if v is not None}
    if preserve:
        preserve = dict(preserve)
        if preserve.get("embedding_text") != doc.get("title"):
            for field in ("embedding", "embedding_model", "embedding_text", "embedded_at"):
                preserve.pop(field, None)
        # ES-owned fields win: a re-derived course type must not overwrite one
        # a person confirmed.
        doc.update(preserve)

    # Stamped after `preserve` only because none of the digested fields are
    # ES-owned — the two sets do not overlap, so the order is a readability
    # choice rather than a correctness one. If a field is ever added to both,
    # this must move above the update or the digest will describe the owners
    # while the document describes the index.
    doc["content_digest"] = content_digest(doc)
    return doc


# Fields `build_document` derives from the owners, and therefore the only ones a
# re-projection may clear. Anything absent from here is either ES-owned or
# derived by `Recipe.validate`, and its absence from a projection says nothing
# about whether it should still exist.
OWNER_PROJECTED_FIELDS: tuple[str, ...] = (
    "title", "description", "instructions", "url", "image_url",
    "source", "source_id", "duration", "serves", "cost_category",
    "cost_category_code", "cost_category_status", "cost_price_coverage", "cost",
    "disabled_at", "ingredients", "ingredient_class_ancestors",
    "allergens", "allergen_evidence", "consumer_suitability",
    "suitable_for", "tags", "diet_tags", "nutrition_claims", "seasonality",
)


# Fields that exist only in Elasticsearch and must survive a re-projection.
ES_OWNED_FIELDS: tuple[str, ...] = (
    "course_types", "cuisines", "flavor_profiles", "moods", "food_groups",
    "annotation_evidence", "enhancements", "ai_generated_fields",
    "ai_allergens", "ai_tags", "embedding", "embedding_model",
    "embedding_text", "embedded_at", "review_status", "visibility",
    "creator", "extras", "planning_tier", "planning_excluded_reason",
    "created_at",
)


# Cypher kept next to its caller: this is the only write this module makes back
# to an owner, and it writes exactly two properties.
_STAMP_DIGEST = """
MATCH (r:Recipe)
WHERE r.recipe_id = $rid OR r.id = $rid
SET r.content_digest = $digest,
    r.projected_at = datetime()
"""


def _stamp_owner_digest(recipe_id: str, digest: str | None) -> None:
    """Record on the Neo4j node what was last projected from it.

    This is what makes drift detectable without reading Elasticsearch at all:
    re-derive the digest from the node and compare it to `content_digest`. If
    they differ, the node changed after its last projection — by some path that
    did not re-project — and the index is serving a stale answer.

    Deliberately not fatal. The projection has already succeeded and the index
    is correct; failing here would turn a bookkeeping problem into a failed
    write. A missing stamp degrades reconciliation to the slower full-document
    comparison, which still finds the drift.
    """
    if not digest:
        return
    try:
        with driver.session() as session:
            session.run(_STAMP_DIGEST, {"rid": recipe_id, "digest": digest})
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not stamp content_digest on %s: %s — reconciliation will "
            "fall back to comparing full documents",
            recipe_id,
            exc,
        )


def project(recipe_id: str, *, refresh: str = "wait_for") -> dict[str, Any]:
    """Rebuild one recipe's catalog document from its owners.

    Raises ``ProjectionError`` rather than returning False. The previous
    implementation's silent failure is exactly how the corpus went stale
    without anyone noticing.
    """
    entity = recipe_entity()
    row = fetch_owner_row(recipe_id)
    if row is None:
        raise ProjectionError(f"recipe {recipe_id} not found in Neo4j")

    existing = entity.get(recipe_id) or {}
    preserve = {
        field: existing[field]
        for field in ES_OWNED_FIELDS
        if existing.get(field) not in (None, [], {}, "")
    }

    # Nutrition lives in Postgres, not Neo4j, so the owner row alone cannot
    # produce it. Omitting this step is why a recipe created through the API was
    # profiled in Postgres and unprofiled everywhere anyone could see — and,
    # because unfiltered browse uses `exists: nutri_score_eu` as its
    # has-been-profiled marker, why it never appeared in browse at all.
    #
    # A nutrition outage must not remove the recipe from search, so a failure
    # here degrades to an unprofiled document rather than to no document.
    try:
        profiles = load_profiles_for(recipe_id, nutri_label=nutri_label)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not load nutrition profiles for %s: %s — projecting without them",
            recipe_id,
            exc,
        )
        profiles = []

    try:
        document = build_document(row, profiles=profiles, preserve=preserve)
        validated = entity.validate(dict(document))
    except Exception as exc:  # noqa: BLE001
        raise ProjectionError(f"could not build document for {recipe_id}: {exc}") from exc

    urn = validated["urn"]
    if existing:
        # Clearing a field on the owner must clear it in the index.
        #
        # `build_document` strips None values and `update` is a partial merge,
        # so a field the owner no longer has is simply absent from the payload
        # and keeps whatever it had before. Removing a recipe's image, url or
        # description left the old value serving indefinitely, and a
        # re-projection could not fix it — the field was absent from that too.
        #
        # Only owner-projected fields are cleared. An ES-owned field missing
        # from `validated` means the projection did not produce it, not that
        # anyone deleted it — nulling those would wipe every annotation on the
        # first patch.
        for field in OWNER_PROJECTED_FIELDS:
            if field in existing and field not in validated:
                validated[field] = None

        # Flat nutrition fields are Postgres-profile-derived, not Neo4j-owned.
        # A partial ES update must explicitly clear any score whose profile no
        # longer exists. Only the four canonical v4 regions are runtime fields;
        # retired USDA/short-Slovenian fields are absent from the strict schema.
        for suffix in ("eu", "ie", "hu", "slovenian"):
            for prefix in ("nutri_score_", "nutri_color_", "nutri_rank_", "nutri_points_"):
                field = f"{prefix}{suffix}"
                if field in existing and field not in validated:
                    validated[field] = None

    try:
        if existing:
            entity.es.update(entity.alias, urn, validated, refresh=refresh)
        else:
            validated = entity.upsert_system_fields(validated, update=False)
            entity.es.index(entity.alias, urn, validated, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise ProjectionError(f"indexing {recipe_id} failed: {exc}") from exc

    _stamp_owner_digest(recipe_id, validated.get("content_digest"))

    logger.info("projected recipe %s into %s", recipe_id, entity.alias)
    return validated


def project_many(recipe_ids: Iterable[str]) -> tuple[int, list[tuple[str, str]]]:
    """Project several recipes; returns (succeeded, [(recipe_id, error)])."""
    ok, failures = 0, []
    for recipe_id in recipe_ids:
        try:
            project(recipe_id, refresh="false")
            ok += 1
        except ProjectionError as exc:
            failures.append((recipe_id, str(exc)))
    if ok:
        recipe_entity().es.refresh(recipe_entity().alias)
    return ok, failures
