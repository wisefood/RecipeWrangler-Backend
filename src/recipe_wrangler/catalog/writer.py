"""The single commit path from a recipe's owners to its search document.

Before this, three unrelated mechanisms kept Elasticsearch in step: `project()`
after create and patch, `sync_recipe_status_to_es` after disable and enable, and
a bulk `_update_by_query` for by-query disables. Each was individually correct
and collectively unaccountable — a write that used none of them left the index
stale with nothing to say so.

This module does not replace the status paths; they are bulk-shaped for good
reason. It replaces the *content* path, and makes it say what happened.

A committed recipe is only finished when it is **stored, profiled and
annotated**. Those three do not fail the same way, so they are not treated the
same way:

``project``
    Rebuilds the document from Neo4j and Postgres. If this fails the recipe is
    invisible to every reader, so it is the one step whose failure is recorded
    loudly and retried by reconciliation.

``annotate``
    An LLM call against Groq. It is slow, it is a third party, and it is the
    difference between a recipe that appears under "Italian" and one that
    exists but can never be found by any facet. Too important to skip and too
    unreliable to block a write on — so it is attempted, and a failure marks
    the recipe pending rather than losing it.

Nothing here is silent. Every partial outcome leaves a marker on the Neo4j
node, which is the owner store and therefore the only place a marker survives
an Elasticsearch problem. `scripts/maintenance/reconcile.py` and
`scripts/catalog/annotate_recipes.py --retry-pending` consume those markers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from recipe_wrangler.utils.neo4j_utils import driver

logger = logging.getLogger(__name__)

# Facets an annotation pass fills. A recipe missing all of them is unreachable
# by every discovery filter the UI offers.
ANNOTATED_FACETS: tuple[str, ...] = (
    "cuisines",
    "moods",
    "flavor_profiles",
    "food_groups",
)

# Markers written to the Neo4j node. On the owner, not the document, because a
# projection failure is exactly when Elasticsearch cannot be trusted to hold
# the record of its own failure.
_SET_PENDING = """
MATCH (r:Recipe)
WHERE r.recipe_id = $rid OR r.id = $rid
SET r.projection_pending = $projection_pending,
    r.annotation_pending = $annotation_pending,
    r.commit_attempted_at = datetime()
"""


@dataclass
class CommitResult:
    """What actually happened, per step.

    Returned rather than raised so a caller can decide: a create endpoint
    reports success with a warning, a backfill script counts failures, and
    reconciliation retries. Collapsing all three into an exception would force
    every caller into the strictest of those readings.
    """

    recipe_id: str
    projected: bool = False
    annotated: bool = False
    annotation_skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    facets: dict[str, list[str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Stored *and* annotated — the only state that needs no follow-up."""
        return self.projected and self.annotated

    def summary(self) -> str:
        parts = [f"projected={self.projected}", f"annotated={self.annotated}"]
        if self.annotation_skipped_reason:
            parts.append(f"skipped={self.annotation_skipped_reason}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def _mark_pending(recipe_id: str, *, projection: bool, annotation: bool) -> None:
    """Record on the owner what still needs doing.

    Best-effort by necessity: if Neo4j is unreachable there is nowhere else to
    write this, and raising would replace a recoverable partial commit with a
    failed one. The log line is the fallback record.
    """
    try:
        with driver.session() as session:
            session.run(
                _SET_PENDING,
                {
                    "rid": recipe_id,
                    "projection_pending": projection,
                    "annotation_pending": annotation,
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record pending flags for %s: %s", recipe_id, exc)


def _existing_facets(document: dict[str, Any]) -> dict[str, list[str]]:
    return {
        facet: list(document.get(facet) or [])
        for facet in ANNOTATED_FACETS
        if document.get(facet)
    }


def annotate(
    recipe_id: str,
    document: dict[str, Any],
    *,
    overwrite: bool = False,
) -> tuple[bool, dict[str, list[str]], str | None]:
    """Fill the discovery facets for one already-projected recipe.

    Returns ``(annotated, facets, skip_reason)``.

    Skips a recipe that already carries facets unless ``overwrite`` is set —
    re-annotating on every patch would burn a model call per edit and, worse,
    silently replace a value a human confirmed. `enhancements[].before` makes
    that reversible, but not free.
    """
    from recipe_wrangler.catalog import annotation as A
    from recipe_wrangler.catalog.entities import recipe_entity

    already = _existing_facets(document)
    if already and not overwrite:
        return False, already, "already annotated"

    title = str(document.get("title") or "").strip()
    if not title:
        # The model classifies from title plus ingredients; without a title the
        # result is noise, and noise in a closed vocabulary is worse than a gap
        # because it is indistinguishable from a real classification.
        return False, {}, "no title to classify"

    ingredients = [
        item.get("name") if isinstance(item, dict) else item
        for item in (document.get("ingredients") or [])
    ]

    facets, confidence = A.suggest(
        title=title,
        ingredients=[str(i) for i in ingredients if i],
        description=document.get("description"),
        tags=document.get("tags") or [],
        source=document.get("source"),
    )
    facets = {k: v for k, v in (facets or {}).items() if v}
    if not facets:
        # A model that returns nothing usable is not an error — it may be a
        # genuine abstention on an ambiguous recipe — but it is also not an
        # annotation, and calling it one would hide the gap.
        return False, {}, "model returned no usable facets"

    patch = dict(facets)
    evidence = A.evidence_for(facets, method="model", confidence=confidence)

    # Food groups are derived from FoodOn ancestry, never guessed — a model-
    # assigned food group is indistinguishable from an ontology-backed one once
    # it is in the index, and the whole value of this facet is that it is not a
    # guess. Empty is the normal result for a recipe whose ingredients have not
    # been linked into the taxonomy yet; the gap is recorded rather than filled
    # with something plausible.
    groups, group_evidence = A.derive_food_groups(document)
    if groups:
        patch["food_groups"] = groups
        evidence = [*evidence, *group_evidence]

    patch["annotation_evidence"] = evidence

    entity = recipe_entity()
    entity.patch(recipe_id, patch, refresh="wait_for", reread=False)
    return True, facets, None


def commit(
    recipe_id: str,
    *,
    annotate_recipe: bool = True,
    overwrite_annotation: bool = False,
    refresh: str = "wait_for",
) -> CommitResult:
    """Project a recipe into the catalog index, then annotate it.

    The order is not arbitrary. Annotation patches a document that must already
    exist, and it reads the projected title, ingredients and tags rather than
    re-querying the owners — so projecting first is what makes the annotation
    describe the recipe as stored rather than as submitted.

    Never raises for a partial outcome. A recipe that projected but did not
    annotate is a real, usable recipe with a gap, and the caller is told which.
    """
    from recipe_wrangler.catalog.projection import ProjectionError, project

    result = CommitResult(recipe_id=recipe_id)

    try:
        document = project(recipe_id, refresh=refresh)
        result.projected = True
    except ProjectionError as exc:
        result.errors.append(f"projection: {exc}")
        logger.error(
            "commit FAILED for %s — saved in Neo4j/Postgres but ABSENT from "
            "search until re-projected: %s",
            recipe_id,
            exc,
        )
        _mark_pending(recipe_id, projection=True, annotation=annotate_recipe)
        return result

    if not annotate_recipe:
        result.annotation_skipped_reason = "not requested"
        _mark_pending(recipe_id, projection=False, annotation=False)
        return result

    try:
        annotated, facets, reason = annotate(
            recipe_id, document, overwrite=overwrite_annotation
        )
        result.annotated = annotated
        result.facets = facets
        result.annotation_skipped_reason = reason
    except Exception as exc:  # noqa: BLE001
        # Groq being slow or down must not fail a write whose data is already
        # safely stored. The recipe is findable by text and filterable by
        # everything the owners provide; only the discovery facets are missing.
        result.errors.append(f"annotation: {exc}")
        logger.warning(
            "annotation failed for %s (recipe is stored and searchable, "
            "facets pending): %s",
            recipe_id,
            exc,
        )

    _mark_pending(
        recipe_id,
        projection=False,
        annotation=not result.annotated and not result.facets,
    )

    logger.info("committed recipe %s — %s", recipe_id, result.summary())
    return result
