#!/usr/bin/env python3
"""Scrape https://supervalu.ie/recipes/healthy into JSON shaped like the
`current catalog` Elasticsearch mapping (scripts/elasticsearch/index_current catalog.py).

Fields the site has that current catalog has NO slot for are kept under
"extra_fields" per recipe (not dropped) — see the printed report at the end.

Usage:
    uv run python scripts/import/web/scrape_supervalu_recipes.py --out supervalu.json
    uv run python scripts/import/web/scrape_supervalu_recipes.py --limit 5
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import requests
from bs4 import BeautifulSoup

BASE = "https://supervalu.ie"
LISTING_URL = f"{BASE}/recipes/healthy"
SOURCE = "SuperValu"
HEADERS = {"User-Agent": "Mozilla/5.0 (RecipeWrangler research scraper)"}
SLEEP_SECONDS = 0.5

session = requests.Session()
session.headers.update(HEADERS)


def _get(url: str) -> BeautifulSoup:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    return BeautifulSoup(resp.text, "html.parser")


def _iso_duration_to_minutes(iso: str | None) -> float | None:
    if not iso:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso)
    if not m:
        return None
    hours, minutes = m.groups()
    total = (int(hours) * 60 if hours else 0) + (int(minutes) if minutes else 0)
    return float(total) if (hours or minutes) else None


def list_recipe_urls() -> list[str]:
    """Paginate via ?limit_from=&limit_per_page= until a page returns no cards."""
    urls: list[str] = []
    page_size = 50
    offset = 0
    while True:
        soup = _get(f"{LISTING_URL}?limit_from={offset}&limit_per_page={page_size}")
        cards = soup.select("div.recipe-list a.item[href]")
        if not cards:
            break
        for a in cards:
            urls.append(BASE + a["href"] if a["href"].startswith("/") else a["href"])
        offset += page_size
    return urls


def fetch_detail(url: str) -> dict:
    soup = _get(url)
    item = soup.select_one(".item")
    title = item.select_one("h1[itemprop=name]").get_text(strip=True) if item else ""
    author_el = item.select_one(".byline .author") if item else None
    image_el = item.select_one("img[itemprop=image]") if item else None
    desc_el = item.select_one("[itemprop=description]") if item else None
    serves_el = item.select_one(".serves") if item else None
    cook_meta = item.select_one("meta[itemprop=cookTime]") if item else None
    prep_meta = item.select_one("meta[itemprop=prepTime]") if item else None

    ingredients = []
    for li in soup.select("li[itemprop=ingredients]"):
        qty = li.select_one(".quantity")
        units = li.select_one(".units")
        name = li.select_one(".ingredient")
        note = li.select_one(".note")
        ingredients.append({
            "quantity": qty.get("data-quantity") if qty else None,
            "units": units.get_text(strip=True) if units else None,
            "ingredient": name.get_text(strip=True) if name else None,
            "note": note.get_text(strip=True) if note else None,
            "sku": li.get("data-sku"),
            "variety_id": li.get("data-variety-id"),
        })

    method_steps = []
    instr = soup.select_one("[itemprop=recipeInstructions]")
    if instr:
        for li in instr.select("li"):
            text = li.get_text(strip=True)
            if text:
                method_steps.append(text)

    serves_text = serves_el.get_text(strip=True) if serves_el else None
    serves_num = re.search(r"\d+", serves_text) if serves_text else None

    return {
        "title": title,
        "author": author_el.get_text(strip=True) if author_el else None,
        "image_url": image_el.get("src") if image_el else None,
        "description": desc_el.get_text(strip=True) if desc_el else None,
        "serves_raw": serves_text,
        "serves": float(serves_num.group()) if serves_num else None,
        "cook_minutes": _iso_duration_to_minutes(cook_meta.get("content") if cook_meta else None),
        "prep_minutes": _iso_duration_to_minutes(prep_meta.get("content") if prep_meta else None),
        "ingredients": ingredients,
        "method_steps": method_steps,
    }


def recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


def build_recipe(url: str, detail: dict) -> dict:
    prep_min = detail["prep_minutes"]
    cook_min = detail["cook_minutes"]
    duration = (prep_min or 0) + (cook_min or 0) or None
    ingredient_lines = [
        " ".join(
            str(part) for part in (i["quantity"], i["units"], i["ingredient"])
            if part
        ) + (f" ({i['note']})" if i["note"] else "")
        for i in detail["ingredients"]
    ]

    return {
        # --- current catalog ES mapping fields ---
        "id": recipe_id(detail["title"]),
        "title": detail["title"],
        "url": url,
        "image_url": detail["image_url"],
        "source": SOURCE,
        "source_id": None,
        "ingredients": ingredient_lines,
        "tags": [],
        "dish_types": [],
        "duration": duration,
        "serves": detail["serves"],
        "cost_category": None,
        # --- fields the site has that current catalog has no column for ---
        "extra_fields": {
            "author": detail["author"],
            "prep_minutes": prep_min,
            "cook_minutes": cook_min,
            "serves_raw": detail["serves_raw"],
            "description": detail["description"],
            "method_steps": detail["method_steps"],
            "ingredients_structured": detail["ingredients"],
        },
    }


EXTRA_FIELD_NOTES = {
    "author": "recipe byline (e.g. 'SuperValu' or a named contributor like 'Niall Breslin') — no author field in current catalog",
    "prep_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "cook_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "serves_raw": "raw text e.g. '15 people' — `serves` above is the parsed number",
    "description": "intro blurb — current catalog has no description/summary field",
    "method_steps": "cooking instructions — not indexed in current catalog at all (lives only in Neo4j)",
    "ingredients_structured": "per-ingredient quantity/units/note/SuperValu product SKU+variety_id — current catalog's `ingredients` is a flat text list, no structured amount or product-catalog linkage",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("supervalu.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print("[scrape] fetching recipe listing...", flush=True)
    urls = list(dict.fromkeys(list_recipe_urls()))
    # Listing sometimes repeats a recipe under a stale /page/NNNN URL
    # alongside its canonical /recipes/<slug> URL — prefer the canonical one.
    urls.sort(key=lambda u: "/recipes/" not in u)
    if args.limit:
        urls = urls[: args.limit]
    print(f"[scrape] {len(urls)} recipes found", flush=True)

    recipes = []
    seen_titles: set[str] = set()
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}", flush=True)
        try:
            detail = fetch_detail(url)
        except Exception as e:
            print(f"    FAIL {e}", flush=True)
            continue
        if detail["title"] in seen_titles:
            print("    skip duplicate title", flush=True)
            continue
        seen_titles.add(detail["title"])
        recipes.append(build_recipe(url, detail))

    args.out.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] wrote {len(recipes)} recipes -> {args.out}", flush=True)

    print("\nExtra fields NOT in the current catalog ES schema (kept under extra_fields):")
    for field, note in EXTRA_FIELD_NOTES.items():
        print(f"  - {field}: {note}")


if __name__ == "__main__":
    main()
