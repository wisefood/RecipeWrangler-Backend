#!/usr/bin/env python3
"""Scrape https://thehungarysoul.com/our-categorie/hungarian-recipes/ into JSON
shaped like the `current catalog` Elasticsearch mapping
(scripts/elasticsearch/index_current catalog.py).

Every recipe page embeds a full schema.org Recipe JSON-LD node inside the
Yoast SEO @graph, so detail extraction is a direct json.loads.

Every recipe is tagged "Hungarian" (same convention as the bestofhungary.co.uk
scrape — this site's own recipeCuisine field also already says "Hungarian").

Fields the site has that current catalog has NO slot for are kept under
"extra_fields" per recipe — see the printed report at the end.

Usage:
    uv run python scripts/import/web/scrape_thehungarysoul_recipes.py --out thehungarysoul.json
    uv run python scripts/import/web/scrape_thehungarysoul_recipes.py --limit 5
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

BASE = "https://thehungarysoul.com"
LISTING_URL = f"{BASE}/our-categorie/hungarian-recipes/"
SOURCE = "The Hungary Soul"
TAG = "Hungarian"
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
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", iso)
    if not m:
        return None
    days, hours, minutes = m.groups()
    if not (days or hours or minutes):
        return None
    return float(int(days or 0) * 1440 + int(hours or 0) * 60 + int(minutes or 0))


def list_recipe_urls() -> list[str]:
    urls: list[str] = []
    page = 1
    while True:
        url = LISTING_URL if page == 1 else f"{LISTING_URL}page/{page}/"
        soup = _get(url)
        links = {
            a["href"] for a in soup.select("a[href*='/recipe-post/']")
            if a.get("href")
        }
        if not links:
            break
        urls.extend(sorted(links))
        page += 1
    return urls


def _flatten_instructions(instructions: list) -> list[str]:
    steps = []
    for entry in instructions or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("@type") == "HowToSection":
            for step in entry.get("itemListElement") or []:
                if isinstance(step, dict) and step.get("text"):
                    steps.append(step["text"])
        elif entry.get("text"):
            steps.append(entry["text"])
    return steps


def fetch_detail(url: str) -> dict | None:
    soup = _get(url)
    script = soup.select_one("script.yoast-schema-graph")
    if not script:
        return None
    data = json.loads(script.string or script.get_text(), strict=False)
    for node in data.get("@graph", []):
        if node.get("@type") == "Recipe":
            return node
    return None


def recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


def build_recipe(url: str, data: dict) -> dict:
    title = data.get("name", "")
    prep_min = _iso_duration_to_minutes(data.get("prepTime"))
    cook_min = _iso_duration_to_minutes(data.get("cookTime"))
    duration = _iso_duration_to_minutes(data.get("totalTime")) or (
        (prep_min or 0) + (cook_min or 0) or None
    )
    yield_list = data.get("recipeYield")
    yield_str = yield_list[0] if isinstance(yield_list, list) and yield_list else yield_list
    serves = None
    if yield_str:
        m = re.search(r"\d+", str(yield_str))
        serves = float(m.group()) if m else None

    images = data.get("image") or []
    image_url = images[0] if isinstance(images, list) and images else images or None

    category = data.get("recipeCategory")
    category = category[0] if isinstance(category, list) and category else category
    cuisine = data.get("recipeCuisine")
    cuisine = cuisine[0] if isinstance(cuisine, list) and cuisine else cuisine

    tags = [TAG]
    if category:
        tags.append(category)

    return {
        # --- current catalog ES mapping fields ---
        "id": recipe_id(title),
        "title": title,
        "url": url,
        "image_url": image_url,
        "source": SOURCE,
        "source_id": None,
        "ingredients": data.get("recipeIngredient") or [],
        "tags": tags,
        "dish_types": [],
        "duration": duration,
        "serves": serves,
        "cost_category": None,
        # --- fields the site has that current catalog has no column for ---
        "extra_fields": {
            "author": (data.get("author") or {}).get("name"),
            "prep_minutes": prep_min,
            "cook_minutes": cook_min,
            "serves_raw": yield_str,
            "description": data.get("description"),
            "method_steps": _flatten_instructions(data.get("recipeInstructions")),
            "recipe_category": category,
            "recipe_cuisine": cuisine,
            "aggregate_rating": data.get("aggregateRating"),
            "nutrition": data.get("nutrition"),
        },
    }


EXTRA_FIELD_NOTES = {
    "author": "named recipe author (e.g. 'Rose Virag') — no author field in current catalog",
    "prep_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "cook_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "serves_raw": "raw recipeYield string — `serves` above is the parsed number",
    "description": "intro blurb — current catalog has no description/summary field",
    "method_steps": "cooking instructions (flattened from WP Recipe Maker's HowToSection groups) — not indexed in current catalog at all",
    "recipe_category": "site's category (e.g. 'Main Course') — folded into `tags` above but flagged, no dedicated category field in current catalog",
    "recipe_cuisine": "schema.org cuisine field (always 'Hungarian' here) — no current catalog equivalent",
    "aggregate_rating": "user star rating + review text — no rating field in current catalog",
    "nutrition": "site-published per-serving nutrition facts (calories, carbs, protein, fat, etc.) — current catalog stores computed nutri_score/nutri_color per region, not raw site nutrient facts",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("thehungarysoul.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print("[scrape] fetching recipe listing...", flush=True)
    urls = list(dict.fromkeys(list_recipe_urls()))
    if args.limit:
        urls = urls[: args.limit]
    print(f"[scrape] {len(urls)} recipes found", flush=True)

    recipes = []
    seen_titles: set[str] = set()
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}", flush=True)
        try:
            data = fetch_detail(url)
        except Exception as e:
            print(f"    FAIL {e}", flush=True)
            continue
        if not data:
            print("    FAIL no Recipe JSON-LD found", flush=True)
            continue
        title = data.get("name", "")
        if title in seen_titles:
            print("    skip duplicate title", flush=True)
            continue
        seen_titles.add(title)
        recipes.append(build_recipe(url, data))

    args.out.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] wrote {len(recipes)} recipes -> {args.out}", flush=True)

    print("\nExtra fields NOT in the current catalog ES schema (kept under extra_fields):")
    for field, note in EXTRA_FIELD_NOTES.items():
        print(f"  - {field}: {note}")


if __name__ == "__main__":
    main()
