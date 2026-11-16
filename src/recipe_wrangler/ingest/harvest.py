"""Walking a list of recipe pages and reading what each one publishes.

The other half of a generalised import. `discovery` says which pages; this
reads them, through the parser the app already uses for "import from a URL".

Two obligations apply here that do not apply to that single-URL path, and the
difference is worth stating because it is the reason this is its own module
rather than a loop around `fetch_recipe_from_url`:

* **robots.txt is honoured.** One person pasting one link is their own visit.
  Fetching eight hundred pages is crawling somebody's site, and a site that
  has asked crawlers to stay out of a section has asked us.
* **Requests are paced.** A source worth importing is usually a small
  organisation on modest hosting. Taking their catalogue as fast as the
  network allows is how an import becomes an outage, and the delay costs us
  nothing that matters — this runs in the background either way.

Nothing here raises for a page that fails. A source of several hundred recipes
will always contain some that are drafts, redirects to a category, or simply
not marked up; the import is the ones that worked plus an honest account of
the ones that did not.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlparse

from recipe_wrangler.utils.recipe_url import (
    USER_AGENT, RecipeUrlError, fetch_bounded, parse_recipe_html,
)

logger = logging.getLogger(__name__)

DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT = 15.0


@dataclass
class PageOutcome:
    """One page, and what came of asking for it."""

    url: str
    ok: bool
    recipe: dict | None = None
    reason: str | None = None
    #: Which fields schema.org had nothing for. A recipe can be worth importing
    #: without a serving count; it is not worth importing without ingredients,
    #: and the parser already refuses that case.
    missing: list[str] = field(default_factory=list)


@dataclass
class HarvestReport:
    outcomes: list[PageOutcome] = field(default_factory=list)
    blocked_by_robots: int = 0
    elapsed_seconds: float = 0.0

    @property
    def recipes(self) -> list[dict]:
        return [o.recipe for o in self.outcomes if o.ok and o.recipe]

    @property
    def failures(self) -> list[PageOutcome]:
        return [o for o in self.outcomes if not o.ok]

    def summary(self) -> dict:
        """What to show a person deciding whether this source is worth having."""
        reasons: dict[str, int] = {}
        for outcome in self.failures:
            key = (outcome.reason or "unknown")[:80]
            reasons[key] = reasons.get(key, 0) + 1
        incomplete = sum(1 for o in self.outcomes if o.ok and o.missing)
        return {
            "attempted": len(self.outcomes),
            "imported": len(self.recipes),
            "failed": len(self.failures),
            "blocked_by_robots": self.blocked_by_robots,
            "incomplete": incomplete,
            "failure_reasons": dict(sorted(reasons.items(),
                                           key=lambda kv: -kv[1])[:10]),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


class _Robots:
    """robots.txt per host, fetched once.

    A host whose robots.txt cannot be read is treated as allowing us. That is
    the convention, and the alternative — refusing a whole source because a
    file 404s — would be wrong far more often than it would be careful.
    """

    def __init__(self, user_agent: str = USER_AGENT, timeout: float = 10.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allows(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._cache:
            self._cache[host] = self._load(host)
        parser = self._cache[host]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url) and parser.can_fetch("*", url)

    def _load(self, host: str):
        try:
            text, _final = fetch_bounded(
                f"{host}/robots.txt", timeout=self.timeout, accept="text/plain",
                content_types=(), max_bytes=512_000)
        except Exception:  # noqa: BLE001 — see the class docstring
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        return parser


def harvest(
    urls: Iterable[str],
    *,
    delay: float = DEFAULT_DELAY_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int | None = None,
    respect_robots: bool = True,
    on_progress: Callable[[PageOutcome], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HarvestReport:
    """Read every page, returning what parsed and why the rest did not.

    `respect_robots` exists as a parameter rather than a constant for one
    legitimate case: a source the platform runs or has written permission
    from, whose robots.txt excludes crawlers generally. It defaults to true and
    turning it off is a decision somebody makes on the record.
    """
    report = HarvestReport()
    robots = _Robots(timeout=timeout) if respect_robots else None
    started = time.monotonic()
    count = 0

    for url in urls:
        if limit is not None and count >= limit:
            break
        count += 1

        if robots is not None and not robots.allows(url):
            report.blocked_by_robots += 1
            outcome = PageOutcome(url=url, ok=False,
                                  reason="robots.txt asks us not to fetch this")
            report.outcomes.append(outcome)
            if on_progress:
                on_progress(outcome)
            continue

        try:
            html, final = fetch_bounded(url, timeout=timeout)
            recipe = parse_recipe_html(html, final)
            outcome = PageOutcome(url=url, ok=True, recipe=recipe,
                                  missing=list(recipe.get("missing_required_fields") or []))
        except RecipeUrlError as exc:
            outcome = PageOutcome(url=url, ok=False, reason=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 — one bad page is not a failed import
            logger.warning("harvest: %s failed", url, exc_info=True)
            outcome = PageOutcome(url=url, ok=False,
                                  reason=f"{type(exc).__name__}: {exc}"[:200])

        report.outcomes.append(outcome)
        if on_progress:
            on_progress(outcome)
        if delay:
            sleep(delay)

    report.elapsed_seconds = time.monotonic() - started
    return report
