#!/usr/bin/env python3
"""Build the ``recipes`` index from its owners: Neo4j content + Postgres profiles.

Replaces the two writers that had to be kept byte-compatible by hand —
``scripts/elasticsearch/index_recipes_v2.py`` (corpus rebuild) and
``utils/es_recipe_projection.py`` (per-recipe refresh). Both reassembled the
document independently, and they disagreed: the offline builder read
``Recipe.meal_type``/``Recipe.dish_type`` properties while the runtime
projection read ``Tag`` nodes with ``category='dish-type'``, so the index ended
up holding the union of two incompatible vocabularies (``main-dish`` *and*
``main_dish``, ``desserts`` *and* ``dessert``). This reads both owners in one
query and canonicalizes through the entity layer, so there is exactly one
definition of a recipe document.

Usage
-----
  # Dry run: assemble everything, write nothing, report what would be indexed
  python scripts/catalog/build_recipes.py --dry-run

  # Build into a new concrete index and swap the alias atomically
  python scripts/catalog/build_recipes.py --new-index recipes_v3 --apply

  # Refresh in place (alias must already exist)
  python scripts/catalog/build_recipes.py --in-place --apply

  # Limit while iterating
  python scripts/catalog/build_recipes.py --sources foodhero,myplate --limit 50
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from sqlalchemy import text

from recipe_wrangler.api.config import get_settings
from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog.elastic import get_catalog_client
from recipe_wrangler.catalog.entities import nutri_label, recipe_entity
from recipe_wrangler.catalog.nutrition import apply_profiles, profile_summary
from recipe_wrangler.catalog.es_schema import recipe_index
from recipe_wrangler.utils.consumer_suitability import (
    SUITABILITY_CLASSIFICATION_VERSION,
)
from recipe_wrangler.utils.es_recipe_evidence import (
    normalize_allergen_evidence,
    normalize_consumer_suitability,
    suitable_groups,
)
from recipe_wrangler.utils.neo4j_utils import driver
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

logger = logging.getLogger("build_recipes")

# One query, both course-type owners. `meal_type`/`dish_type` are properties
# carried only by the Hungarian and Slovenian imports; the Tag edges are what
# every other source uses. Reading both here is what collapses the two
# vocabularies into one.
RECIPE_QUERY = """
MATCH (r:Recipe)
WHERE ($sources IS NULL OR r.source IN $sources)
WITH r ORDER BY coalesce(r.recipe_id, r.id)
SKIP $skip LIMIT $limit
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
  RETURN collect(DISTINCT i.name) AS ingredients,
         collect(DISTINCT i.canonical_id) AS ingredient_ids
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
  RETURN collect(DISTINCT al.name) AS allergens
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_CLASS]->(:FoodOnClass)
                 -[:SUBCLASS_OF*0..5]->(anc:FoodOnClass)
  RETURN collect(DISTINCT anc.foodon_id) AS ingredient_class_ancestors
}
CALL { WITH r
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
CALL { WITH r
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
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
  RETURN collect(DISTINCT t.name) AS tags,
         collect(DISTINCT CASE WHEN t.category = 'dish-type' THEN t.name END) AS tag_dish_types,
         collect(DISTINCT CASE WHEN t.category IN ['dietary','dietary_option'] THEN t.name END) AS diet_tags
}
// Text properties are returned RAW rather than toString()'d. The corpus is not
// consistent about scalar-vs-array: `instructions` is a StringArray of steps on
// most sources, `seasonality` is a StringArray of seasons, and toString() throws
// on an array rather than coercing. Normalization happens in Python where both
// shapes can be handled.
RETURN
  coalesce(r.recipe_id, r.id) AS recipe_id,
  r.id AS internal_id,
  r.title AS title,
  r.description AS description,
  r.instructions AS instructions,
  r.url AS url,
  r.image_url AS image_url,
  r.source AS source,
  r.source_id AS source_id,
  coalesce(r.duration_minutes, r.duration) AS duration,
  r.serves AS serves,
  r.cost_category AS cost_category,
  coalesce(r.expert_recipe, false) AS expert_recipe,
  coalesce(r.status, "active") AS status,
  toString(r.disabled_at) AS disabled_at,
  coalesce(r.has_profile, false) AS has_profile,
  coalesce(r.has_rcsi_lab_nutrition, false) AS has_rcsi_nutrition,
  coalesce(r.has_planeat_nutrition, false) AS has_planeat_nutrition,
  r.ground_truth_nutrition_source AS ground_truth_nutrition_source,
  r.meal_type AS meal_type,
  r.dish_type AS dish_type,
  ingredients, ingredient_ids, allergens, ingredient_class_ancestors,
  allergen_evidence, consumer_suitability, tags, tag_dish_types, diet_tags
"""


# Fields that exist ONLY in Elasticsearch — nothing in Neo4j or Postgres can
# reproduce them. A rebuild reads the owners, so without carrying these across
# every rebuild silently destroys the annotation work: ~7,200 recipes of
# cuisines, flavour profiles, moods and course types, plus the FoodOn-derived
# food groups and the provenance that makes them auditable.
ES_OWNED_FIELDS: tuple[str, ...] = (
    "course_types",
    "cuisines",
    "flavor_profiles",
    "moods",
    "food_groups",
    "annotation_evidence",
    "enhancements",
    "ai_generated_fields",
    "ai_allergens",
    "ai_tags",
    "embedding",
    "embedding_model",
    "embedding_text",
    "embedded_at",
    "review_status",
    "visibility",
    "creator",
    "extras",
)


def load_carry_over(alias: str) -> dict[str, dict[str, Any]]:
    """Read the ES-only fields out of the live index, keyed by recipe_id.

    Streamed with search_after rather than from/size so corpus growth cannot
    push it past a result window.
    """
    client = get_catalog_client()
    if not (client.alias_exists(alias) or client.index_exists(alias)):
        logger.info("carry-over: %s does not exist yet, nothing to carry", alias)
        return {}

    carried: dict[str, dict[str, Any]] = {}
    for hit in client.scroll_all(
        alias, source=["recipe_id", *ES_OWNED_FIELDS], page_size=1000
    ):
        src = hit.get("_source") or {}
        recipe_id = _clean(src.get("recipe_id"))
        if not recipe_id:
            continue
        kept = {
            field: src[field]
            for field in ES_OWNED_FIELDS
            if src.get(field) not in (None, [], {}, "")
        }
        if kept:
            carried[recipe_id] = kept
    return carried


def _clean(value: object) -> str:
    text_value = str(value).strip() if value is not None else ""
    return "" if text_value in {"null", "None"} else text_value


def _clean_text(value: object) -> str:
    """Flatten a text property that may be a string or a list of steps.

    Sources disagree: most store `instructions` as a StringArray, one step per
    element; others store a single string. Joining with newlines preserves the
    step boundaries for display without the caller needing to care.
    """
    if isinstance(value, (list, tuple)):
        parts = [_clean(part) for part in value]
        return "\n".join(part for part in parts if part)
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


def stream_recipes(
    sources: list[str] | None, batch_size: int, limit: int | None
) -> Iterator[dict[str, Any]]:
    """Page through Neo4j rather than holding the corpus in memory."""
    skip, yielded = 0, 0
    while True:
        page = batch_size if limit is None else min(batch_size, limit - yielded)
        if page <= 0:
            return
        with driver.session() as session:
            rows = session.run(
                RECIPE_QUERY,
                {
                    "sources": sources,
                    "skip": skip,
                    "limit": page,
                    "suitability_version": SUITABILITY_CLASSIFICATION_VERSION,
                },
            ).data()
        if not rows:
            return
        for row in rows:
            yield row
            yielded += 1
        skip += len(rows)
        if limit is not None and yielded >= limit:
            return


def load_profiles() -> dict[str, list[dict[str, Any]]]:
    """Profile summaries per recipe, for the denormalized `profiles` array."""
    table = _get_config()["profiles_table"]
    by_recipe: dict[str, list[dict[str, Any]]] = {}
    with get_connection() as conn:
        rows = conn.execute(
            text(
                f'SELECT recipe_id, nutrition_source, source, nutri_score, '
                f'       total_sustainability_per_serving, pipeline_version, computed_at '
                f'FROM "{table}"'
            )
        ).mappings()
        for row in rows:
            by_recipe.setdefault(_clean(row["recipe_id"]), []).append(
                profile_summary(row, nutri_label=nutri_label)
            )
    return by_recipe


def build_document(
    row: dict[str, Any], profiles: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assemble one recipe document from its owners.

    Only the raw material is put together here; canonicalization of sources,
    course types, ingredients and the default score all happen in
    ``Recipe.validate`` so a single write and a bulk write cannot diverge.
    """
    recipe_id = _clean(row["recipe_id"]) or _clean(row["internal_id"])

    # Both course-type owners feed the same list; the entity folds them.
    # `meal_type`/`dish_type` are scalars on the sources that carry them, but
    # handled as either shape for the same reason the query returns them raw.
    course_candidates = list(_clean_list(row.get("tag_dish_types")))
    for key in ("meal_type", "dish_type"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            course_candidates.extend(_clean_list(value))
        elif _clean(value):
            course_candidates.append(_clean(value).lower())

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
        "external_id": _clean(row.get("source_id")) or None,
        "duration": _float(row.get("duration")),
        "serves": _float(row.get("serves")),
        "cost_category": _clean(row.get("cost_category")) or None,
        "expert_recipe": bool(row.get("expert_recipe")),
        "status": _clean(row.get("status")) or "active",
        "disabled_at": _clean(row.get("disabled_at")) or None,
        "has_profile": bool(profiles) or bool(row.get("has_profile")),
        "has_rcsi_nutrition": bool(row.get("has_rcsi_nutrition")),
        "has_planeat_nutrition": bool(row.get("has_planeat_nutrition")),
        "ground_truth_nutrition_source": _clean(
            row.get("ground_truth_nutrition_source")
        )
        or None,
        "ingredients": _clean_list(row.get("ingredients")),
        "ingredient_class_ancestors": _clean_list(
            row.get("ingredient_class_ancestors")
        ),
        "allergens": _clean_list(row.get("allergens")),
        "allergen_evidence": normalize_allergen_evidence(row.get("allergen_evidence")),
        "consumer_suitability": consumer,
        "suitable_for": suitable_groups(consumer),
        "tags": _clean_list(row.get("tags")),
        "diet_tags": _clean_list(row.get("diet_tags")),
        "course_types": course_candidates,
    }

    # Shared with the per-recipe commit path. The rebuild and a single create
    # must produce the same document, or a rebuild would silently rewrite what
    # a create had just written.
    apply_profiles(doc, profiles)

    return {k: v for k, v in doc.items() if v is not None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", help="Comma-separated source slugs (default: all active)")
    ap.add_argument("--limit", type=int, help="Stop after N recipes.")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--new-index", help="Build into this concrete index, then swap the alias.")
    ap.add_argument("--in-place", action="store_true", help="Write into the existing alias.")
    ap.add_argument("--alias", default=None, help="Override the recipes alias.")
    ap.add_argument(
        "--carry-over",
        dest="carry_over",
        action="store_true",
        default=True,
        help="Preserve Elasticsearch-only fields (annotations, provenance, "
             "embeddings) from the live index. On by default.",
    )
    ap.add_argument(
        "--no-carry-over",
        dest="carry_over",
        action="store_false",
        help="Rebuild purely from owners, DISCARDING all annotation work.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Assemble but do not write.")
    ap.add_argument("--apply", action="store_true", help="Actually write.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s",
    )

    if not args.apply:
        args.dry_run = True
    if not args.dry_run and not (args.new_index or args.in_place):
        ap.error("choose --new-index <name> or --in-place")

    settings = get_settings()
    alias = args.alias or settings.catalog_recipes_alias

    raw_sources = None
    if args.sources:
        raw_sources = []
        for slug in args.sources.split(","):
            source = S.resolve(slug.strip())
            if source is None:
                ap.error(f"unknown source: {slug}")
            raw_sources.append(source.raw)
    else:
        # Never rebuild retired sources back into the corpus.
        raw_sources = [s.raw for s in S.active_sources()]

    logger.info("sources=%s alias=%s", raw_sources, alias)

    carried: dict[str, dict[str, Any]] = {}
    if args.carry_over:
        logger.info("loading carry-over fields from %s...", alias)
        carried = load_carry_over(alias)
        logger.info("carry-over: %s recipe(s) with ES-only fields", len(carried))
    else:
        logger.warning(
            "--no-carry-over: annotations and provenance in %s will NOT survive",
            alias,
        )

    logger.info("loading profiles from postgres...")
    profiles_by_recipe = load_profiles()
    logger.info(
        "loaded profiles for %s recipes (%s rows)",
        len(profiles_by_recipe),
        sum(len(v) for v in profiles_by_recipe.values()),
    )

    client = get_catalog_client()
    entity = recipe_entity()
    target = alias

    if not args.dry_run and args.new_index:
        logger.info("creating %s", args.new_index)
        client._request(
            "PUT", args.new_index, body=recipe_index(settings.catalog_embedding_dim)
        )
        target = args.new_index
        entity = type(entity)(alias=args.new_index, register=False)
    elif not args.dry_run:
        client.ensure_indices()

    stats = Counter()
    per_source = Counter()
    course_hist = Counter()
    batch: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal batch
        if not batch or args.dry_run:
            batch = []
            return
        ok, errs = entity.bulk_upsert(batch, refresh=False)
        stats["indexed"] += ok
        failures.extend(errs)
        batch = []

    for row in stream_recipes(raw_sources, args.batch_size, args.limit):
        try:
            recipe_id = _clean(row["recipe_id"])
            doc = build_document(row, profiles_by_recipe.get(recipe_id, []))

            # Carried fields win over anything re-derived from the owners. A
            # reannotated course_type must not be overwritten by the scraped
            # source tag it was correcting.
            preserved = carried.get(recipe_id)
            if preserved:
                doc.update(preserved)
                stats["carried_over"] += 1

            validated = entity.validate(dict(doc, urn=doc["urn"]))
        except Exception as exc:  # a bad row must not abort the corpus
            stats["skipped"] += 1
            logger.warning("skipping %s: %s", row.get("recipe_id"), exc)
            continue

        stats["assembled"] += 1
        per_source[validated.get("source", "?")] += 1
        for course in validated.get("course_types", []) or []:
            course_hist[course] += 1
        if validated.get("has_profile"):
            stats["with_profile"] += 1
        if validated.get("default_nutri_score"):
            stats["with_default_score"] += 1

        batch.append(doc)
        if len(batch) >= args.batch_size:
            flush()
    flush()

    if not args.dry_run:
        client.refresh(target)
        if args.new_index:
            # The documents are already in `new_index`; the alias just has to
            # start pointing at it. Reindexing into yet another index (as an
            # earlier draft did) would duplicate the corpus for no reason.
            old = client.point_alias(alias, args.new_index)
            logger.info(
                "alias %s now -> %s (was %s)", alias, args.new_index, old or "unset"
            )
            if old:
                logger.info("previous index %s retained; delete when satisfied", old)

    logger.info("--- summary ---")
    for key, value in sorted(stats.items()):
        logger.info("%-20s %s", key, value)
    logger.info("by source: %s", dict(per_source))
    logger.info("course_types: %s", dict(course_hist))
    if failures:
        logger.error("%s bulk failure(s); first: %s", len(failures), failures[0])
    if args.dry_run:
        logger.info("dry-run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
