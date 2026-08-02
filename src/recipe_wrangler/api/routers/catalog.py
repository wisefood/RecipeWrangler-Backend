"""Catalog-backed recipe endpoints — the uniform search contract.

Served from the ``recipes`` alias (recipes_v3) through the catalog entity layer,
alongside the legacy ``/api/v1`` routes which still read ``recipes_v2``. Both
run at once so the read flip can be done per-consumer rather than as one
cut-over.

The contract is deliberately identical to wisefood-data-api's ``SearchSchema``
— ``q``, ``fq``, ``fl``, ``sort``, ``facets``, ``limit``, ``offset`` returning
``{results, facets, total, max_result_window}`` — so one client and one UI
search component work against the catalog and the recipe corpus alike.

Unlike ``/api/v1/recipes/search`` there is no LLM in the request path. That
endpoint sends every query to a constraint extractor first, which is why a bare
noun like "pasta" could come back with no constraints at all and match the whole
corpus. Here the caller states what it wants: ``q`` ranks, ``fq`` filters.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog.entities import recipe_entity
from recipe_wrangler.api.exceptions import NotFoundError as HTTPNotFound
from recipe_wrangler.catalog.entity import NotFoundError, ValidationError

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.identity import (
    Caller,
    get_caller,
    redact,
    status_filter,
    visible_to,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/recipes", tags=["catalog"])


class CatalogSearchRequest(BaseModel):
    """Contract-identical to wisefood-data-api's SearchSchema."""

    q: str | None = Field(None, description="Free-text query. Ranks; never filters.")
    fq: list[str] = Field(
        default_factory=list,
        description=(
            "Filter queries in Lucene syntax, ANDed together. "
            'e.g. ["course_types:desserts", "food_groups:fruit", "duration:[* TO 30]"]'
        ),
    )
    fl: list[str] = Field(
        default_factory=list, description="Fields to return. Empty returns all."
    )
    sort: list[str] = Field(
        default_factory=list, description='e.g. ["default_nutri_rank:asc", "title.kw:asc"]'
    )
    facets: list[str] = Field(
        default_factory=list,
        description="Keyword fields to facet on. Any mapped keyword field works.",
    )
    facet_limit: int = Field(30, ge=1, le=200)
    limit: int = Field(20, ge=0, le=200)
    offset: int = Field(0, ge=0)
    include_inactive: bool = False
    highlight: list[str] = Field(default_factory=list)


def _sort_clauses(sort: list[str]) -> list[dict[str, Any]]:
    """Parse ``field:direction`` into Elasticsearch sort clauses."""
    out: list[dict[str, Any]] = []
    for entry in sort:
        field, _, direction = str(entry).partition(":")
        field = field.strip()
        if not field:
            continue
        direction = (direction or "asc").strip().lower()
        if direction not in {"asc", "desc"}:
            direction = "asc"
        out.append({field: {"order": direction}})
    return out


def _filter_clauses(fq: list[str]) -> list[dict[str, Any]]:
    """Lucene filter strings to ES clauses.

    ``query_string`` rather than hand-parsed terms so ranges, negation and
    boolean grouping all work without this layer needing to know about them.
    """
    return [
        {"query_string": {"query": entry, "default_operator": "AND"}}
        for entry in fq
        if str(entry).strip()
    ]


@router.post("/search", summary="Search recipes (catalog contract)")
def catalog_search(
    payload: CatalogSearchRequest, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    """Search the catalog index. Returns {results, facets, total, max_result_window}."""
    entity = recipe_entity()
    try:
        # Withdrawn recipes are visible to experts only, and `include_inactive`
        # is honoured only for them — otherwise it is a self-service escalation.
        result = entity.search(
            q=payload.q or None,
            filters=_filter_clauses(payload.fq)
            + status_filter(caller, include_inactive=payload.include_inactive),
            limit=payload.limit,
            offset=payload.offset,
            sort=_sort_clauses(payload.sort) or None,
            source_fields=payload.fl or None,
            facets=payload.facets or None,
            facet_limit=payload.facet_limit,
            include_inactive=True,  # scoping is done by status_filter above
            highlight_fields=payload.highlight or None,
        )
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc
    return redact(result, caller)


@router.get("/facets", summary="Facetable fields available on the index")
def catalog_facets() -> dict[str, Any]:
    """List every field that can be passed in `facets`.

    Derived from the mapping, so a newly added keyword field becomes facetable
    without a code change.
    """
    entity = recipe_entity()
    try:
        fields = entity.es.facet_fields(entity.alias)
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc
    return {"index": entity.alias, "count": len(fields), "fields": fields}


@router.get("/browse", summary="Browse by category")
def catalog_browse(
    course_type: str | None = Query(None, description="e.g. desserts, main-dish, soup"),
    cuisine: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = Query(24, ge=1, le=200),
    offset: int = Query(0, ge=0),
    caller: Caller = Depends(get_caller),
) -> dict[str, Any]:
    """The query behind Browse-by-Category.

    Filter values are canonicalized on the way in, so a caller asking for
    ``dessert`` and an index holding ``desserts`` still agree.
    """
    entity = recipe_entity()
    try:
        result = entity.browse(
            course_type=course_type,
            cuisine=cuisine,
            source=source,
            q=q,
            limit=limit,
            offset=offset,
        )
        return redact(result, caller)
    except ValidationError as exc:
        raise map_dependency_error("catalog browse", exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc


@router.get("/vocabulary", summary="Controlled vocabularies for filtering")
def catalog_vocabulary() -> dict[str, Any]:
    """The closed value sets the UI should offer, rather than free text."""
    from recipe_wrangler.catalog import vocabularies as V

    return {
        "classification_version": V.CLASSIFICATION_VERSION,
        "course_types": list(S.COURSE_TYPES),
        "sources": [
            {"slug": s.slug, "name": s.display_name, "curated": s.curated}
            for s in S.active_sources()
        ],
        **{
            facet: {
                "values": list(spec["values"]),
                "method": spec["method"],
                "authoritative": spec["authoritative"],
            }
            for facet, spec in V.FACETS.items()
        },
    }


@router.get("/{recipe_id}", summary="Get one recipe from the catalog index")
def catalog_get(
    recipe_id: str, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    entity = recipe_entity()
    try:
        document = entity.get_or_raise(recipe_id)
    except NotFoundError:
        # A genuine 404, not a dependency failure. Routing this through
        # map_dependency_error returned 503, which tells a client the service
        # is broken when the recipe simply does not exist.
        raise HTTPNotFound(detail=f"recipe {recipe_id} not found") from None
    except Exception as exc:  # noqa: BLE001
        raise map_dependency_error("Elasticsearch", exc) from exc

    if not visible_to(document, caller):
        # Indistinguishable from absent: confirming that a withdrawn recipe
        # exists is itself a disclosure.
        raise HTTPNotFound(detail=f"recipe {recipe_id} not found")
    return redact(document, caller)
