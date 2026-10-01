"""Discover current HealthyFoods recipe URLs from sitemaps and listings."""

# data/HealthyFoods/export_healthyfoods_recipe_urls.py
from __future__ import annotations

import json
import re
import sys
from typing import List, Set, Iterable, Optional
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


SITE = "https://www.healthyfood.com"
LISTING = "https://www.healthyfood.com/healthy-recipes/"
RECIPE_PREFIX = "https://www.healthyfood.com/healthy-recipes/"
OUT_JSON = "data/HealthyFoods/recipe_urls.json"


# ---------------------------
# Helpers
# ---------------------------

def _norm(u: str) -> str:
    u = u.strip()
    u = u.split("#")[0].split("?")[0]
    if not u.endswith("/"):
        u += "/"
    return u


def _is_recipe_url(u: str) -> bool:
    if not u.startswith(RECIPE_PREFIX):
        return False
    if "/healthy-recipes/page/" in u:
        return False
    if u.rstrip("/") == RECIPE_PREFIX.rstrip("/"):
        return False
    return True


# ---------------------------
# 1) SITEMAP approach (best)
# ---------------------------

def fetch_text(session: requests.Session, url: str, timeout: int = 30) -> Optional[str]:
    r = session.get(url, timeout=timeout)
    if r.status_code != 200:
        return None
    return r.text


def parse_sitemap_xml(xml_text: str) -> List[str]:
    """
    Returns list of <loc> URLs from a sitemap XML (either sitemapindex or urlset).
    Handles namespaces.
    """
    root = ET.fromstring(xml_text)
    # Namespace handling: element tags may look like "{ns}urlset"
    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    tag = strip_ns(root.tag)
    locs: List[str] = []

    if tag == "sitemapindex":
        for sm in root.findall(".//{*}sitemap"):
            loc = sm.find("{*}loc")
            if loc is not None and loc.text:
                locs.append(loc.text.strip())
    elif tag == "urlset":
        for url_el in root.findall(".//{*}url"):
            loc = url_el.find("{*}loc")
            if loc is not None and loc.text:
                locs.append(loc.text.strip())
    else:
        # unknown, still try to pull any loc
        for loc in root.findall(".//{*}loc"):
            if loc is not None and loc.text:
                locs.append(loc.text.strip())

    return locs


def extract_recipe_urls_from_sitemaps(session: requests.Session) -> Set[str]:
    """
    Tries common WP sitemap locations:
      - /sitemap_index.xml (Yoast / RankMath / etc.)
      - /sitemap.xml
      - /wp-sitemap.xml (WordPress core)
    Then recursively downloads all referenced sitemaps and extracts recipe URLs.
    """
    candidates = [
        urljoin(SITE, "/sitemap_index.xml"),
        urljoin(SITE, "/sitemap.xml"),
        urljoin(SITE, "/wp-sitemap.xml"),
    ]

    seen_sitemaps: Set[str] = set()
    to_visit: List[str] = []

    # seed
    for u in candidates:
        txt = fetch_text(session, u)
        if txt and "<" in txt and "sitemap" in txt.lower():
            to_visit.append(u)

    all_urls: Set[str] = set()

    while to_visit:
        sm_url = to_visit.pop()
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        txt = fetch_text(session, sm_url)
        if not txt:
            continue

        try:
            locs = parse_sitemap_xml(txt)
        except Exception:
            # Sometimes HTML error page; skip
            continue

        # If this is a sitemapindex, locs are other sitemap URLs.
        # If this is a urlset, locs are page URLs.
        # We can distinguish by quick check: if most locs end with ".xml", treat as sitemaps.
        xmlish = sum(1 for x in locs[:50] if x.strip().lower().endswith(".xml"))
        if xmlish >= max(1, len(locs[:50]) // 2):
            for child in locs:
                if child.lower().endswith(".xml") and child not in seen_sitemaps:
                    to_visit.append(child)
        else:
            for page_url in locs:
                u = _norm(page_url)
                if _is_recipe_url(u):
                    all_urls.add(u)

    return all_urls


# ---------------------------
# 2) Fallback: initial HTML only
# ---------------------------

def extract_from_initial_listing(session: requests.Session) -> Set[str]:
    r = session.get(LISTING, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links: Set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        href = urljoin(SITE, href)
        href = _norm(href)
        if _is_recipe_url(href):
            links.add(href)

    return links


def main() -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    # 1) Sitemap (should get EVERYTHING if exposed)
    urls = extract_recipe_urls_from_sitemaps(session)
    if urls:
        print(f"[sitemap] found {len(urls)} recipe urls")
    else:
        print("[sitemap] nothing found (missing/blocked). Falling back to initial HTML only.")
        urls = extract_from_initial_listing(session)
        print(f"[html] found {len(urls)} recipe urls (initial batch only)")

    out: List[str] = sorted(urls)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(out)} recipe URLs to {OUT_JSON}")


if __name__ == "__main__":
    main()
