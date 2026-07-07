#!/usr/bin/env python3
"""Scrape recipes from https://www.safefood.net/recipes and save to Excel + JSON.

safefood.net sits behind a Cloudflare JS challenge, so plain HTTP gets a 403 —
a real browser is required. This uses Playwright (headless Chromium) with a
single reused browser across the whole run.

Setup (uv-managed venv):
    uv pip install requests beautifulsoup4 lxml openpyxl pandas playwright
    .venv/bin/python -m playwright install chromium

Run:
    PYTHONPATH=src .venv/bin/python scripts/scrape_safefood_recipes.py
    # one category, capped, for a quick test:
    .venv/bin/python scripts/scrape_safefood_recipes.py --categories breakfast --limit 3

Output (in exports/ by default):
    safefood_web_recipes.xlsx   # title + url first, then details/nutrition
    safefood_web_recipes.json   # full structured backup
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.safefood.net"
CATEGORIES = ["breakfast", "lunch", "dinner", "snacks", "desserts"]
CATEGORY_PATHS = {c: f"{BASE_URL}/recipes/{c}" for c in CATEGORIES}

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "exports"
OUTPUT_BASENAME = "safefood_web_recipes"

DELAY_SECONDS = 1.5          # polite crawl delay between page loads
CF_WAIT_MS = 6000            # let the Cloudflare challenge resolve after load
NAV_TIMEOUT_MS = 45000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── Data model ─────────────────────────────────────────────────────────────────


@dataclass
class NutritionInfo:
    energy_kj: Optional[str] = None
    energy_kcal: Optional[str] = None
    fat_g: Optional[str] = None
    saturates_g: Optional[str] = None
    sugars_g: Optional[str] = None
    salt_g: Optional[str] = None
    five_a_day: Optional[str] = None


@dataclass
class Recipe:
    name: str
    url: str
    category: Optional[str] = None
    description: Optional[str] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    serves: Optional[str] = None
    ingredients: list[str] = field(default_factory=list)
    method: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    nutrition: NutritionInfo = field(default_factory=NutritionInfo)
    image_url: Optional[str] = None


# ── Browser fetch (Playwright, single reused instance) ─────────────────────────


@contextmanager
def browser_fetcher(headless: bool = True):
    """Yield a `fetch(url) -> BeautifulSoup | None` backed by one Chromium page."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=USER_AGENT,
                                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})

        def fetch(url: str) -> Optional[BeautifulSoup]:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(CF_WAIT_MS)
                html = page.content()
                if "just a moment" in html.lower():
                    print(f"  [CF] challenge not cleared: {url}", file=sys.stderr)
                    return None
                return BeautifulSoup(html, "lxml")
            except Exception as e:  # one bad page must not kill the run
                print(f"  [ERROR] {url}: {str(e)[:120]}", file=sys.stderr)
                return None

        try:
            yield fetch
        finally:
            browser.close()


# ── Parsing helpers ────────────────────────────────────────────────────────────


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _container_items(soup: BeautifulSoup, selector: str) -> list[str]:
    """All non-empty <li> texts inside any element matching `selector`.

    Deduplicates by element identity so a div+ul pair both matching the selector
    doesn't yield each <li> twice. Order preserved.
    """

    seen: set[int] = set()
    out: list[str] = []
    for container in soup.select(selector):
        for li in container.find_all("li"):
            if id(li) in seen:
                continue
            seen.add(id(li))
            txt = _clean(li.get_text())
            if txt:
                out.append(txt)
    return out


def get_recipe_urls(soup: BeautifulSoup) -> list[str]:
    """Recipe links on a category listing.

    Two URL shapes appear: the common `/recipes/<slug>` and a two-segment
    `/recipes/<category>/<slug>` (e.g. `/recipes/lunch/chicken-soup`). Both are
    accepted; bare category pages (`/recipes/lunch`) and pagination links are
    excluded.
    """

    cat_paths = {f"/recipes/{c}" for c in CATEGORIES} | {"/recipes"}
    pattern = re.compile(r"/recipes/[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?$")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if pattern.fullmatch(href) and href not in cat_paths:
            full = BASE_URL + href
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out


def _parse_nutrition(full_text: str) -> NutritionInfo:
    n = NutritionInfo()
    m = re.search(r"Energy\s*([\d.]+)\s*kj", full_text, re.I)
    if m:
        n.energy_kj = m.group(1) + "kJ"
    m = re.search(r"([\d.]+)\s*kcal", full_text, re.I)
    if m:
        n.energy_kcal = m.group(1) + "kcal"
    for label, attr in (("Fat", "fat_g"), ("Saturates", "saturates_g"),
                        ("Sugars", "sugars_g"), ("Salt", "salt_g")):
        m = re.search(label + r"\s*([\d.]+)\s*g", full_text, re.I)
        if m:
            setattr(n, attr, m.group(1) + "g")
    m = re.search(r"(\d+)\s+of your\s+5\s+a\s+day", full_text, re.I)
    if m:
        n.five_a_day = m.group(0)
    return n


def parse_recipe(url: str, soup: BeautifulSoup, category: Optional[str]) -> Recipe:
    r = Recipe(name="", url=url, category=category)

    h1 = soup.find("h1")
    r.name = _clean(h1.get_text()) if h1 else url.rsplit("/", 1)[-1].replace("-", " ").title()

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = og["content"]
        r.image_url = BASE_URL + src if src.startswith("/") else src

    # Ingredients — the `.recipe-ingredients` container is sometimes a <ul>,
    # sometimes a <div> wrapping a class-less <ul>; collect <li> from either and
    # dedup by element identity so nested container matches aren't double-counted.
    r.ingredients = _container_items(soup, ".recipe-ingredients")

    # Method — the first real <ol> on the page (nav/menus use <ul>).
    for ol in soup.find_all("ol"):
        steps = [_clean(li.get_text()) for li in ol.find_all("li")]
        steps = [s for s in steps if s]
        if steps:
            r.method = steps
            break

    # Equipment — `.recipe-details` container, skipping the "Print Recipe" list.
    r.equipment = [i for i in _container_items(soup, ".recipe-details")
                   if "print recipe" not in i.lower()]

    full_text = soup.get_text(" ", strip=True)
    time_re = r"(\d[\d\s]*(?:min|hr|hour|minute)s?)"
    for label, attr in (("Prep", "prep_time"), ("Cook", "cook_time"), ("Total", "total_time")):
        m = re.search(label + r"\s*Time[:\s]+" + time_re, full_text, re.I)
        if m:
            setattr(r, attr, _clean(m.group(1)))
    m = re.search(r"Serves?[:\s]+(\d+)", full_text, re.I)
    if m:
        r.serves = m.group(1)

    r.nutrition = _parse_nutrition(full_text)
    return r


# ── Output ─────────────────────────────────────────────────────────────────────


def write_outputs(recipes: list[Recipe], out_dir: Path, basename: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{basename}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in recipes], f, indent=2, ensure_ascii=False)
    print(f"Saved {len(recipes)} recipes → {json_path}")

    rows = []
    for r in recipes:
        d = asdict(r)
        n = d.pop("nutrition")
        rows.append({
            "name": d["name"],
            "url": d["url"],                       # link included, up front
            "category": d["category"],
            "description": d["description"],
            "prep_time": d["prep_time"],
            "cook_time": d["cook_time"],
            "total_time": d["total_time"],
            "serves": d["serves"],
            "ingredients": " | ".join(d["ingredients"]),
            "method": " | ".join(d["method"]),
            "equipment": " | ".join(d["equipment"]),
            **n,
            "image_url": d["image_url"],
        })
    xlsx_path = out_dir / f"{basename}.xlsx"
    pd.DataFrame(rows).to_excel(xlsx_path, index=False)
    print(f"Saved {len(recipes)} recipes → {xlsx_path}")


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default=",".join(CATEGORIES),
                    help="comma-separated subset of: " + ", ".join(CATEGORIES))
    ap.add_argument("--limit", type=int, default=None,
                    help="cap recipes per category (smoke test)")
    ap.add_argument("--out", default=str(OUTPUT_DIR), help="output directory")
    ap.add_argument("--basename", default=OUTPUT_BASENAME, help="output file basename")
    ap.add_argument("--headed", action="store_true", help="run a visible browser")
    args = ap.parse_args()

    categories = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    out_dir = Path(args.out)

    recipes: list[Recipe] = []
    with browser_fetcher(headless=not args.headed) as fetch:
        # 1) collect recipe URLs per category, walking ?page=N until a page is empty
        url_to_cat: dict[str, str] = {}
        for cat in categories:
            base = CATEGORY_PATHS.get(cat, f"{BASE_URL}/recipes/{cat}")
            cat_urls: list[str] = []
            page = 1
            while True:
                listing = f"{base}?page={page}"
                soup = fetch(listing)
                page_urls = get_recipe_urls(soup) if soup else []
                # Stop on an empty page or one that adds nothing new (last page loops back).
                new = [u for u in page_urls if u not in url_to_cat and u not in cat_urls]
                print(f"Listing {cat} page {page}: {len(page_urls)} recipes ({len(new)} new)")
                if not new:
                    break
                cat_urls.extend(new)
                if args.limit and len(cat_urls) >= args.limit:
                    cat_urls = cat_urls[: args.limit]
                    break
                page += 1
                time.sleep(DELAY_SECONDS)
            for u in cat_urls:
                url_to_cat.setdefault(u, cat)
            print(f"  {cat}: {len(cat_urls)} recipes total")
            time.sleep(DELAY_SECONDS)

        print(f"\nTotal unique recipes to scrape: {len(url_to_cat)}\n")

        # 2) scrape each recipe page
        for i, (url, cat) in enumerate(url_to_cat.items(), 1):
            print(f"[{i}/{len(url_to_cat)}] {url}")
            soup = fetch(url)
            if not soup:
                continue
            try:
                r = parse_recipe(url, soup, cat)
                recipes.append(r)
                print(f"  ✓ {r.name} | ing={len(r.ingredients)} steps={len(r.method)} "
                      f"kcal={r.nutrition.energy_kcal} serves={r.serves}")
            except Exception as e:
                print(f"  [PARSE ERROR] {url}: {str(e)[:120]}", file=sys.stderr)
            time.sleep(DELAY_SECONDS)

    if recipes:
        write_outputs(recipes, out_dir, args.basename)
    else:
        print("No recipes scraped — nothing written.", file=sys.stderr)


if __name__ == "__main__":
    main()
