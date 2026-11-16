"""Finding the recipe pages a source publishes.

Why this exists: importing a source today means writing a script for it. Nine
of them live in ``scripts/``, each three to four hundred lines, and each one
hand-rolls CSS selectors — ``soup.select``, ``find_all("ol")``, ``h1`` — for
one site's markup. That is the reason a new source is a week's work rather
than an afternoon.

Almost none of that is necessary. Recipe sites publish ``schema.org/Recipe``
as JSON-LD because search engines require it, and
``utils.recipe_url.parse_recipe_html`` has read that for a while — it is what
backs "import from a URL" in the app. The parsing was never the hard part.
What was missing is the boring half: *which pages*. This is that half.

Three shapes cover essentially everything a site offers, and none of them
needs per-site code:

* **sitemaps**, including sitemap indexes, which is how a site tells crawlers
  what it has;
* **RSS and Atom feeds**, which is how it announces what is new;
* **a plain list of URLs**, for the case where somebody already has one.

What this does *not* do is walk a site's HTML looking for links. That is
crawling, it needs judgement about what counts as a recipe page, and it is how
an importer turns into a scraper nobody can reason about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from recipe_wrangler.utils.recipe_url import RecipeUrlError, fetch_bounded

logger = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 10_000_000
"""Sitemaps are legitimately large; the spec's own ceiling is 50 MB."""

MAX_SITEMAP_DEPTH = 2
"""A sitemap index of indexes is legal and is also how a fetch becomes a crawl."""

DEFAULT_LIMIT = 5_000

#: A DOCTYPE in a document we did not write is never something we want. Python's
#: ElementTree does not resolve external entities, but it does expand internal
#: ones, which is all "billion laughs" needs. Refusing the declaration outright
#: is cheaper and more obviously correct than reasoning about expansion limits.
_DOCTYPE = re.compile(r"<!DOCTYPE|<!ENTITY", re.I)


class DiscoveryError(ValueError):
    """The document could not be read as a source of URLs."""


@dataclass
class Discovered:
    """What a source turned out to publish."""

    kind: str
    """sitemap | sitemap-index | rss | atom | list"""
    urls: list[str] = field(default_factory=list)
    considered: int = 0
    """How many URLs the document held before filtering."""
    sources: list[str] = field(default_factory=list)
    """Every document actually read, including nested sitemaps."""
    truncated: bool = False
    """True when the limit cut the list short, so a caller can say so."""


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_xml(text: str):
    if _DOCTYPE.search(text[:4000]):
        raise DiscoveryError(
            "that document declares a DOCTYPE or entities, which a sitemap has "
            "no reason to; refusing to parse it")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise DiscoveryError(f"that document is not valid XML: {exc}") from exc


def _urls_from_xml(root, base: str) -> tuple[str, list[str], list[str]]:
    """(kind, page urls, nested sitemap urls) from a sitemap or a feed."""
    kind = _localname(root.tag)

    if kind == "sitemapindex":
        nested = [
            loc.text.strip()
            for entry in root
            for loc in entry
            if _localname(loc.tag) == "loc" and (loc.text or "").strip()
        ]
        return "sitemap-index", [], nested

    if kind == "urlset":
        return "sitemap", [
            loc.text.strip()
            for entry in root
            for loc in entry
            if _localname(loc.tag) == "loc" and (loc.text or "").strip()
        ], []

    if kind == "rss":
        return "rss", [
            link.text.strip()
            for item in root.iter()
            if _localname(item.tag) == "item"
            for link in item
            if _localname(link.tag) == "link" and (link.text or "").strip()
        ], []

    if kind == "feed":
        urls = []
        for entry in root.iter():
            if _localname(entry.tag) != "entry":
                continue
            for link in entry:
                if _localname(link.tag) != "link":
                    continue
                # Atom puts the URL in an attribute, and an entry may carry
                # several links; the alternate is the human-readable page.
                rel = (link.get("rel") or "alternate").lower()
                href = (link.get("href") or "").strip()
                if rel == "alternate" and href:
                    urls.append(urljoin(base, href))
                    break
        return "atom", urls, []

    raise DiscoveryError(
        f"<{kind}> is not a sitemap or a feed this understands; expected "
        f"urlset, sitemapindex, rss or feed")


def _urls_from_text(text: str, base: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(urljoin(base, line))
    return out


def _matches(url: str, include: re.Pattern | None, exclude: re.Pattern | None) -> bool:
    if include and not include.search(url):
        return False
    if exclude and exclude.search(url):
        return False
    return True


def discover(
    location: str,
    *,
    include: str | None = None,
    exclude: str | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: float = 15.0,
    same_host_only: bool = True,
) -> Discovered:
    """Read a sitemap, feed or URL list and return the pages worth trying.

    ``include`` is usually how a site's whole sitemap becomes a recipe import:
    most sitemaps list everything, and a pattern like ``/recipes/`` is the
    difference between eight hundred recipe pages and eight thousand pages of
    which most are not recipes. Filtering here rather than after fetching is
    the point — the pages we skip are pages we never ask the site for.

    ``same_host_only`` defends against a sitemap that points elsewhere. A
    source vouches for its own pages; it does not vouch for a third party's,
    and following one would quietly attribute somebody else's recipes to it.
    """
    include_re = re.compile(include, re.I) if include else None
    exclude_re = re.compile(exclude, re.I) if exclude else None

    text, final = fetch_bounded(
        location, timeout=timeout,
        accept="application/xml,text/xml,application/rss+xml,text/plain;q=0.8",
        content_types=("xml", "text/plain", "rss", "atom"),
        max_bytes=MAX_DOCUMENT_BYTES,
    )
    host = urlparse(final).netloc.lower()

    read: list[str] = [final]
    stripped = text.lstrip()
    if stripped.startswith("<"):
        kind, urls, nested = _urls_from_xml(_parse_xml(text), final)
        depth = 0
        while nested and depth < MAX_SITEMAP_DEPTH and len(urls) < limit:
            depth += 1
            following, nested = nested, []
            for child in following:
                if len(urls) >= limit:
                    break
                if same_host_only and urlparse(child).netloc.lower() != host:
                    continue
                try:
                    child_text, child_final = fetch_bounded(
                        child, timeout=timeout,
                        accept="application/xml,text/xml",
                        content_types=("xml",), max_bytes=MAX_DOCUMENT_BYTES)
                except (RecipeUrlError, DiscoveryError) as exc:
                    # One unreadable sitemap in an index is not a failure of
                    # the import; it is one part of the site we cannot see.
                    logger.warning("discovery: %s could not be read: %s", child, exc)
                    continue
                read.append(child_final)
                _child_kind, child_urls, child_nested = _urls_from_xml(
                    _parse_xml(child_text), child_final)
                urls.extend(child_urls)
                nested.extend(child_nested)
    else:
        kind = "list"
        urls = _urls_from_text(text, final)

    considered = len(urls)
    seen: set[str] = set()
    kept: list[str] = []
    for url in urls:
        if same_host_only and urlparse(url).netloc.lower() != host:
            continue
        if not _matches(url, include_re, exclude_re):
            continue
        if url in seen:
            continue
        seen.add(url)
        kept.append(url)
        if len(kept) >= limit:
            break

    return Discovered(kind=kind, urls=kept, considered=considered,
                      sources=read, truncated=len(kept) >= limit < considered)


def urls_from_list(values: Iterable[str]) -> list[str]:
    """A caller who already has the URLs. Deduplicated, order kept."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out
