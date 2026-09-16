"""Importing a whole source, as an operation somebody can start and watch.

Until now importing a source meant writing a script for it. This is the same
work as an endpoint: point it at a sitemap or a feed, and it discovers the
pages, reads the schema.org markup each one publishes, and runs every recipe
through the existing create-and-profile chain.

Privileged only. It writes recipes into the corpus and fetches at length from
somebody else's site; neither is a participant-facing action.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from recipe_wrangler.api.identity import Caller, get_caller
from recipe_wrangler.ingest import pipeline, runs
from recipe_wrangler.api.exceptions import AuthorizationError, DataError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


class SourceImportRequest(BaseModel):
    location: str = Field(
        description="A sitemap, sitemap index, RSS/Atom feed, or a plain list of URLs")
    source_slug: str | None = Field(
        default=None, description="Which registry source this fills, if one")
    region: str = Field(default="IE", max_length=8)
    include: str | None = Field(
        default=None,
        description="Keep only URLs matching this pattern — usually how a site's "
                    "whole sitemap becomes a recipe import")
    exclude: str | None = None
    limit: int = Field(default=200, ge=1, le=5000)
    delay: float = Field(default=1.0, ge=0, le=30)
    respect_robots: bool = True
    dry_run: bool = Field(
        default=True,
        description="Read the pages without writing recipes. Start here: it says "
                    "how many of a source's pages actually carry usable markup.")


def _require_privileged(caller: Caller) -> None:
    if not caller.is_privileged:
        raise AuthorizationError(detail="Importing a source is an expert action.")


@router.post("/source", summary="Start importing a source from its sitemap or feed")
async def start_source_import(
    payload: SourceImportRequest, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    """Begin an import and return a run to poll.

    Returns immediately because profiling a recipe is seconds and a source is
    hundreds of them; a request that waited would time out long before the
    work did.
    """
    _require_privileged(caller)
    try:
        pipeline.check_capacity()
    except pipeline.ImportRefused as exc:
        raise DataError(detail=str(exc), extra={"title": "ImportRefused"}) from exc

    source = pipeline.resolve_source(payload.source_slug)
    if payload.source_slug and source is None:
        raise DataError(
            detail=f"{payload.source_slug!r} is not a known source. Add it to the "
                   f"registry first, so the recipes it creates resolve to it.",
            extra={"title": "UnknownSource"})

    run_id = runs.new_run_id()
    runs.create(runs.RunState(
        id=run_id, location=payload.location,
        source_slug=source.slug if source else None,
        dry_run=payload.dry_run, started_by=caller.sub,
    ))

    asyncio.create_task(pipeline.run_import(
        run_id=run_id, location=payload.location,
        source_slug=source.slug if source else None,
        region=payload.region, include=payload.include, exclude=payload.exclude,
        limit=payload.limit, delay=payload.delay, dry_run=payload.dry_run,
        respect_robots=payload.respect_robots, started_by=caller.sub,
    ))
    return {"message": "Success", "run": runs.get(run_id)}


@router.get("/source/runs", summary="Recent source imports, newest first")
async def list_source_imports(
    limit: int = Query(default=20, ge=1, le=100),
    caller: Caller = Depends(get_caller),
) -> dict[str, Any]:
    _require_privileged(caller)
    return {"message": "Success", "runs": runs.recent(limit=limit)}


@router.get("/source/runs/{run_id}", summary="One source import and its progress")
async def get_source_import(
    run_id: str, caller: Caller = Depends(get_caller)
) -> dict[str, Any]:
    _require_privileged(caller)
    run = runs.get(run_id)
    if run is None:
        raise DataError(detail="No such import run.", extra={"title": "NotFound"})
    return {"message": "Success", "run": run}


@router.get("/sources", summary="The source registry, with what each licence permits")
async def list_sources(caller: Caller = Depends(get_caller)) -> dict[str, Any]:
    """Every registered source and its licence state.

    Surfaced because `None` means undetermined rather than permissive, and a
    corpus's undetermined sources are its exposure — a number nobody is shown
    is a number that stays where it is.
    """
    _require_privileged(caller)
    from recipe_wrangler.catalog import sources as registry

    return {
        "message": "Success",
        "sources": [
            {
                "slug": s.slug, "display_name": s.display_name,
                "collection_urn": s.collection_urn, "license": s.license,
                "license_url": s.license_url, "attribution": s.attribution,
                "curated": s.curated, "trusted": s.trusted, "retired": s.retired,
            }
            for s in registry.SOURCES
        ],
        "undetermined": sorted(registry.undetermined_licence_slugs()),
    }
