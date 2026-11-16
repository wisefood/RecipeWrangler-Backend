"""Sitemap to profiled recipes, in one run.

`discovery` finds the pages, `harvest` reads them, and `recipe_create` — the
existing seven-step chain that splits ingredients, estimates weights, profiles
nutrition, detects allergens, writes Neo4j, saves the Postgres trace and
indexes Elasticsearch — turns each one into a recipe. None of that is new.
What is new is that the three are connected, so importing a source is a
request rather than a script somebody writes.

Async rather than threaded, deliberately. `recipe_create` is a coroutine and
the profiling underneath it reaches Postgres, Neo4j and Elasticsearch through
the application's own clients; running it from a worker thread would mean a
second event loop touching connections that belong to the first. The blocking
parts — fetching pages — go to a thread instead, which is the direction that
is safe.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from recipe_wrangler.catalog import sources as source_registry
from recipe_wrangler.ingest import discovery, runs
from recipe_wrangler.ingest.harvest import DEFAULT_DELAY_SECONDS
from recipe_wrangler.utils.recipe_url import (
    RecipeUrlError, fetch_bounded, parse_recipe_html,
)

logger = logging.getLogger(__name__)

#: How often progress is flushed. Every recipe would be a write per profiling
#: run, which is pointless when a single recipe can take ten seconds; never
#: would leave a watcher staring at zero for an hour.
FLUSH_EVERY = 5

MAX_CONCURRENT_RUNS = 2
"""Each run holds a share of the profiling chain, which is the expensive part
of this service. Two at once is a ceiling on that, not on politeness."""


class ImportRefused(RuntimeError):
    """The run was not started, and why."""


def _robots():
    from recipe_wrangler.ingest.harvest import _Robots

    return _Robots()


async def _read_page(url: str) -> dict[str, Any]:
    html, final = await asyncio.to_thread(fetch_bounded, url)
    return parse_recipe_html(html, final)


async def run_import(
    *,
    run_id: str,
    location: str,
    source_slug: str | None = None,
    region: str = "IE",
    include: str | None = None,
    exclude: str | None = None,
    limit: int = 200,
    delay: float = DEFAULT_DELAY_SECONDS,
    dry_run: bool = False,
    respect_robots: bool = True,
    started_by: str | None = None,
    create_recipe: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Discover, read and profile a source. Returns the finished run state.

    `dry_run` stops after reading the pages. It is the setting to use first:
    it says how many of a source's pages actually carry schema.org markup,
    which is the one thing that decides whether a source is importable at all,
    and it costs nothing but the fetches.
    """
    state = runs.RunState(
        id=run_id, location=location, source_slug=source_slug,
        status="running", stage="discovering", dry_run=dry_run,
        started_by=started_by,
    )
    runs.save(state)

    try:
        found = await asyncio.to_thread(
            discovery.discover, location,
            include=include, exclude=exclude, limit=limit)
    except Exception as exc:  # noqa: BLE001
        state.status = "failed"
        state.error = f"{type(exc).__name__}: {exc}"[:400]
        runs.save(state, finished=True)
        return state.__dict__

    state.discovered = len(found.urls)
    state.detail["discovery"] = {
        "kind": found.kind, "considered": found.considered,
        "truncated": found.truncated, "documents": found.sources[:10],
    }
    state.stage = "reading"
    runs.save(state)

    if not found.urls:
        state.status = "failed"
        state.error = (
            f"{found.kind} held {found.considered} URLs and none matched. "
            f"Check the include pattern, or point at a sitemap that lists recipes.")
        runs.save(state, finished=True)
        return state.__dict__

    robots = _robots() if respect_robots else None
    reasons: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    if create_recipe is None and not dry_run:
        create_recipe = _default_create(started_by)

    for index, url in enumerate(found.urls, start=1):
        state.attempted = index

        if robots is not None and not await asyncio.to_thread(robots.allows, url):
            state.skipped += 1
            reasons["robots.txt asks us not to fetch this"] = (
                reasons.get("robots.txt asks us not to fetch this", 0) + 1)
        else:
            try:
                recipe = await _read_page(url)
                missing = list(recipe.get("missing_required_fields") or [])
                if missing:
                    # The same bar the single-URL import applies: a recipe with
                    # no instructions is not one we can profile or show.
                    state.failed += 1
                    key = f"missing {', '.join(missing)}"
                    reasons[key] = reasons.get(key, 0) + 1
                elif dry_run:
                    state.imported += 1
                    if len(samples) < 5:
                        samples.append({"url": url, "title": recipe["title"],
                                        "ingredients": len(recipe["ingredients"])})
                else:
                    await create_recipe(recipe, region)
                    state.imported += 1
                    if len(samples) < 5:
                        samples.append({"url": url, "title": recipe["title"],
                                        "ingredients": len(recipe["ingredients"])})
            except RecipeUrlError as exc:
                state.failed += 1
                key = str(exc)[:80]
                reasons[key] = reasons.get(key, 0) + 1
            except Exception as exc:  # noqa: BLE001 — one page is not the import
                logger.warning("source import: %s failed", url, exc_info=True)
                state.failed += 1
                key = f"{type(exc).__name__}: {exc}"[:80]
                reasons[key] = reasons.get(key, 0) + 1

        state.detail["reasons"] = dict(
            sorted(reasons.items(), key=lambda kv: -kv[1])[:10])
        state.detail["samples"] = samples
        if index % FLUSH_EVERY == 0:
            runs.save(state)
        if delay:
            await asyncio.sleep(delay)

    state.status = "succeeded"
    state.stage = "done"
    runs.save(state, finished=True)
    return state.__dict__


def _default_create(started_by: str | None):
    """Adapt a parsed page to the existing create-and-profile chain.

    Imported late and built here rather than in the router so the pipeline can
    be exercised without the API: every test below passes its own
    `create_recipe`, and this is only what production uses.
    """
    from recipe_wrangler.api.identity import Caller
    from recipe_wrangler.api.routers.recipes import recipe_create
    from recipe_wrangler.schemas.models import RecipeCreateRequest

    caller = Caller(sub=started_by, roles=frozenset({"admin"}))

    async def create(recipe: dict[str, Any], region: str):
        return await recipe_create(
            RecipeCreateRequest(
                title=recipe["title"],
                ingredients=recipe["ingredients"],
                instructions=recipe["instructions"],
                duration=recipe["duration"],
                serves=recipe["serves"],
                region=region,
                image_url=recipe.get("image_url"),
                url=recipe["url"],
            ),
            caller,
        )

    return create


def check_capacity() -> None:
    if runs.active_count() >= MAX_CONCURRENT_RUNS:
        raise ImportRefused(
            f"{MAX_CONCURRENT_RUNS} source imports are already running, which is "
            f"the limit. Each one is profiling recipes, and that is the expensive "
            f"part of this service.")


def resolve_source(slug: str | None):
    """The registry entry, so a run records which source it was filling."""
    return source_registry.resolve(slug) if slug else None
