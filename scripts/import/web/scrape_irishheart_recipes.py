#!/usr/bin/env python3
"""Scrape https://irishheart.ie/how-to-keep-your-heart-healthy/recipes/ into
JSON shaped like the `current catalog` Elasticsearch mapping
(scripts/elasticsearch/index_current catalog.py).

Fields the site has that current catalog has NO slot for are kept under
"extra_fields" per recipe (not dropped) — see the printed report at the end,
also emitted next to the requested output JSON.

Usage:
    uv run python scripts/import/web/scrape_irishheart_recipes.py --out irishheart_recipes.json
    uv run python scripts/import/web/scrape_irishheart_recipes.py --limit 5
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

BASE = "https://irishheart.ie"
LISTING_URL = f"{BASE}/how-to-keep-your-heart-healthy/recipes/"
SOURCE = "Irish Heart Foundation"
HEADERS = {"User-Agent": "Mozilla/5.0 (RecipeWrangler research scraper)"}
SLEEP_SECONDS = 0.5

TAGS = [
    "breakfasts", "cooking-with-kids", "desserts", "main-meals", "fish",
    "freezer-friendly", "in-season", "light-meals", "meats", "vegetarian",
]

session = requests.Session()
session.headers.update(HEADERS)


def _get(url: str) -> BeautifulSoup:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    time.sleep(SLEEP_SECONDS)
    return BeautifulSoup(resp.text, "html.parser")


def _minutes(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text)
    return float(m.group(1)) if m else None


def _difficulty(el) -> str | None:
    span = el.find("span", class_=re.compile(r"^difficulty_level_"))
    if not span:
        return None
    return span.get_text(strip=True)


def iter_listing_cards(tag: str | None = None):
    """Yield (url, title, image_url, prep_short, difficulty) for every card,
    paginating recipe_page=1,2,... until a page has none."""
    page = 1
    while True:
        url = f"{LISTING_URL}?recipe_page={page}" + (f"&tag={tag}" if tag else "")
        soup = _get(url)
        cards = soup.select("div.recipelist div.col")
        if not cards:
            break
        for card in cards:
            link = card.select_one(".text a.link, .img-container a.link")
            if not link or not link.get("href"):
                continue
            title_el = card.select_one("h4")
            img_el = card.select_one(".img")
            prep_el = card.select_one(".meta .prep")
            yield {
                "url": link["href"].rstrip("/") + "/",
                "title": title_el.get_text(strip=True) if title_el else "",
                "image_url": _bg_image(img_el),
                "prep_short": prep_el.get_text(strip=True) if prep_el else None,
                "difficulty": _difficulty(card),
            }
        page += 1


def _bg_image(img_el) -> str | None:
    if not img_el:
        return None
    m = re.search(r"url\(([^)]+)\)", img_el.get("style", ""))
    return m.group(1) if m else None


def fetch_tag_membership() -> dict[str, set[str]]:
    """url -> set(tag slugs), via the 10 known tag filters."""
    membership: dict[str, set[str]] = {}
    for tag in TAGS:
        for card in iter_listing_cards(tag=tag):
            membership.setdefault(card["url"], set()).add(tag)
    return membership


def fetch_detail(url: str) -> dict:
    soup = _get(url)
    info = soup.select_one(".info")
    prep_el = info.select_one(".prep") if info else None
    cook_el = info.select_one(".cook") if info else None
    people_el = info.select_one(".people") if info else None
    desc_el = info.select_one(".content") if info else None

    prep_min = _minutes(prep_el.get_text() if prep_el else None)
    cook_min = _minutes(cook_el.get_text() if cook_el else None)
    serves_raw = people_el.get_text(strip=True).replace("serves", "").strip() if people_el else None

    ingredients_block = soup.select_one(".ingredients p")
    ingredient_lines = []
    if ingredients_block:
        raw_html = ingredients_block.decode_contents()
        ingredient_lines = [
            BeautifulSoup(line, "html.parser").get_text(strip=True)
            for line in re.split(r"<br\s*/?>", raw_html)
        ]
        ingredient_lines = [line for line in ingredient_lines if line]

    method_el = soup.select_one(".method")
    method_steps = []
    if method_el:
        for p in method_el.find_all("p", recursive=False):
            text = p.get_text(strip=True)
            if text:
                method_steps.append(text)

    nutrition = {}
    for span in soup.select(".nutrition .details span"):
        value_el = span.find("strong")
        if not value_el:
            continue
        value = value_el.get_text(strip=True)
        label = span.get_text(strip=True).replace(value, "").strip().lower()
        if label:
            nutrition[label] = value

    return {
        "description": desc_el.get_text(strip=True) if desc_el else None,
        "prep_minutes": prep_min,
        "cook_minutes": cook_min,
        "serves_raw": serves_raw,
        "difficulty": _difficulty(info) if info else None,
        "ingredient_lines": ingredient_lines,
        "method_steps": method_steps,
        "nutrition_per_portion": nutrition,
    }


def _serves_to_float(serves_raw: str | None) -> float | None:
    if not serves_raw:
        return None
    nums = [float(n) for n in re.findall(r"\d+", serves_raw)]
    return sum(nums) / len(nums) if nums else None


def recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


def build_recipe(card: dict, detail: dict, tags: set[str]) -> dict:
    title = card["title"]
    prep_min = detail["prep_minutes"]
    cook_min = detail["cook_minutes"]
    duration = (prep_min or 0) + (cook_min or 0) or None

    return {
        # --- current catalog ES mapping fields ---
        "id": recipe_id(title),
        "title": title,
        "url": card["url"],
        "image_url": card["image_url"],
        "source": SOURCE,
        "source_id": None,
        "ingredients": detail["ingredient_lines"],
        "tags": sorted(tags),
        "dish_types": [],
        "duration": duration,
        "serves": _serves_to_float(detail["serves_raw"]),
        "cost_category": None,
        # --- fields the site has that current catalog has no column for ---
        "extra_fields": {
            "vibe": detail["difficulty"] or card["difficulty"],
            "prep_minutes": prep_min,
            "cook_minutes": cook_min,
            "serves_raw": detail["serves_raw"],
            "description": detail["description"],
            "method_steps": detail["method_steps"],
            "nutrition_per_portion": detail["nutrition_per_portion"],
        },
    }


EXTRA_FIELD_NOTES = {
    "vibe": "site's own effort label (Super Easy / Not Too Tricky / Showing Off) — no equivalent current catalog field",
    "prep_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "cook_minutes": "current catalog only has one combined `duration`, not separate prep/cook",
    "serves_raw": "site gives serving ranges like '6-8'; `serves` above is the numeric average",
    "description": "intro blurb — current catalog has no description/summary field",
    "method_steps": "cooking instructions — not indexed in current catalog at all (lives only in Neo4j)",
    "nutrition_per_portion": "site's own published per-portion nutrition facts — current catalog stores computed nutri_score/nutri_color per region, not raw nutrient facts",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("irishheart_recipes.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print("[scrape] fetching tag membership...", flush=True)
    tag_membership = fetch_tag_membership()

    print("[scrape] fetching recipe listing...", flush=True)
    cards = list(iter_listing_cards())
    seen = set()
    unique_cards = []
    for c in cards:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        unique_cards.append(c)
    if args.limit:
        unique_cards = unique_cards[: args.limit]
    print(f"[scrape] {len(unique_cards)} recipes found", flush=True)

    recipes = []
    for i, card in enumerate(unique_cards, 1):
        print(f"[{i}/{len(unique_cards)}] {card['title']}", flush=True)
        try:
            detail = fetch_detail(card["url"])
        except Exception as e:
            print(f"    FAIL {e}", flush=True)
            continue
        tags = tag_membership.get(card["url"], set())
        recipes.append(build_recipe(card, detail, tags))

    args.out.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] wrote {len(recipes)} recipes -> {args.out}", flush=True)

    print("\nExtra fields NOT in the current catalog ES schema (kept under extra_fields):")
    for field, note in EXTRA_FIELD_NOTES.items():
        print(f"  - {field}: {note}")


if __name__ == "__main__":
    main()
