#!/usr/bin/env python3
"""Scrape https://www.slovenian-kitchen.com/recipes into JSON shaped like the
`current catalog` Elasticsearch mapping (scripts/elasticsearch/index_current catalog.py).

The site is Squarespace. All posts live in one `blog-1` collection, fetched
whole via the ?format=json-pretty trick (offset-paginated) — this also
returns each post's full body HTML in the same call, so no per-recipe fetch
is needed. Non-recipe posts (categories "Food History", "Slovenian
Traditions") are excluded.

Site categories (Meat and Fish, Soups and Stews, Sides and Salads, etc.) are
kept as `dish_types`; the site's own freeform SEO tags go to `tags`. If a
recipe's own tags don't already mention "slovenian" (case-insensitive), the
tag "Slovenian" is appended.

Fields the site has that current catalog has NO slot for are kept under
"extra_fields" per recipe — see the printed report at the end.

Usage:
    uv run python scripts/import/web/scrape_slovenian_kitchen_recipes.py --out slovenian-kitchen.json
    uv run python scripts/import/web/scrape_slovenian_kitchen_recipes.py --limit 5
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

BASE = "https://www.slovenian-kitchen.com"
COLLECTION_URL = f"{BASE}/blog-1"
SOURCE = "Slovenian Kitchen"
NON_RECIPE_CATEGORIES = {"food history", "slovenian traditions"}
HEADERS = {"User-Agent": "Mozilla/5.0 (RecipeWrangler research scraper)"}
SLEEP_SECONDS = 0.4

session = requests.Session()
session.headers.update(HEADERS)


def fetch_all_items() -> list[dict]:
    items: list[dict] = []
    offset = None
    while True:
        url = f"{COLLECTION_URL}?format=json-pretty"
        if offset:
            url += f"&offset={offset}"
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        time.sleep(SLEEP_SECONDS)
        data = resp.json()
        items.extend(data["items"])
        pagination = data.get("pagination") or {}
        if not pagination.get("nextPage"):
            break
        offset = pagination["nextPageOffset"]
    return items


_INGREDIENT_RE = re.compile(r"\bingredient", re.I)
_METHOD_RE = re.compile(r"\b(instruction|method|direction)", re.I)


def parse_recipe_body(body_html: str) -> tuple[list[str], list[str]]:
    """Returns (ingredients, method_steps).

    Recipes can have multiple ingredients/instructions sections (e.g. dough,
    sauce, toppings), sometimes merged into the same Squarespace content div,
    so this walks every element in document order and tracks which heading
    ("Ingredients:" / "Instructions:") was most recently seen, appending each
    <li> it meets to whichever list is currently active — rather than trying
    to split by block boundaries, which don't reliably align with sections.
    """
    soup = BeautifulSoup(body_html, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()

    ingredients: list[str] = []
    method_steps: list[str] = []
    mode: str | None = None
    for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "li"]):
        if el.name == "li":
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            if mode == "ingredients":
                ingredients.append(text)
            elif mode == "method":
                method_steps.append(text)
            continue
        if el.find_parent("li"):
            continue  # already covered by the <li> branch above
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 150:
            continue
        if _INGREDIENT_RE.search(text):
            mode = "ingredients"
        elif _METHOD_RE.search(text):
            mode = "method"

    # Some recipes use dish-specific subsection names ("Dough", "Filling")
    # instead of a literal "Ingredients:" heading, so the keyword pass above
    # can leave one side empty even when the other matched. Fall back on the
    # site's consistent list-type convention: <ul> holds ingredients, <ol>
    # holds numbered instruction steps.
    if not ingredients:
        ingredients = [
            li.get_text(" ", strip=True)
            for ul in soup.find_all("ul")
            for li in ul.find_all("li")
            if li.get_text(strip=True)
        ]
    if not method_steps:
        method_steps = [
            li.get_text(" ", strip=True)
            for ol in soup.find_all("ol")
            for li in ol.find_all("li")
            if li.get_text(strip=True)
        ]
    return ingredients, method_steps


def recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


def build_recipe(item: dict) -> dict:
    title = item["title"]
    url = BASE + item["fullUrl"]
    ingredients, method_steps = parse_recipe_body(item.get("body") or "")

    tags = list(item.get("tags") or [])
    if not any("slovenian" in t.lower() for t in tags):
        tags.append("Slovenian")

    excerpt_html = item.get("excerpt") or ""
    description = BeautifulSoup(excerpt_html, "html.parser").get_text(" ", strip=True) or None

    return {
        # --- current catalog ES mapping fields ---
        "id": recipe_id(title),
        "title": title,
        "url": url,
        "image_url": item.get("assetUrl"),
        "source": SOURCE,
        "source_id": None,
        "ingredients": ingredients,
        "tags": tags,
        "dish_types": list(item.get("categories") or []),
        "duration": None,
        "serves": None,
        "cost_category": None,
        # --- fields the site has that current catalog has no column for ---
        "extra_fields": {
            "author": item.get("author"),
            "description": description,
            "method_steps": method_steps,
            "published_on": item.get("publishOn"),
        },
    }


EXTRA_FIELD_NOTES = {
    "author": "named recipe author — no author field in current catalog",
    "description": "intro blurb/excerpt — current catalog has no description/summary field",
    "method_steps": "cooking instructions — not indexed in current catalog at all (lives only in Neo4j)",
    "published_on": "publish timestamp — no equivalent current catalog field",
}
MISSING_FIELD_NOTE = (
    "This site never publishes prep/cook time or serving size, so `duration` "
    "and `serves` are null for every recipe (not a parsing failure)."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("slovenian-kitchen.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print("[scrape] fetching blog-1 collection...", flush=True)
    items = fetch_all_items()
    recipe_items = [
        i for i in items
        if not (set(c.lower() for c in (i.get("categories") or [])) & NON_RECIPE_CATEGORIES)
    ]
    print(f"[scrape] {len(items)} posts total, {len(recipe_items)} are recipes", flush=True)
    if args.limit:
        recipe_items = recipe_items[: args.limit]

    recipes = []
    no_ingredients = []
    for i, item in enumerate(recipe_items, 1):
        print(f"[{i}/{len(recipe_items)}] {item['title']}", flush=True)
        recipe = build_recipe(item)
        if not recipe["ingredients"]:
            no_ingredients.append(recipe["title"])
        recipes.append(recipe)

    args.out.write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[scrape] wrote {len(recipes)} recipes -> {args.out}", flush=True)

    if no_ingredients:
        print(f"\n[warn] {len(no_ingredients)} recipes had no parsed ingredients (check manually):")
        for t in no_ingredients:
            print(f"  - {t}")

    print(f"\n{MISSING_FIELD_NOTE}")
    print("\nExtra fields NOT in the current catalog ES schema (kept under extra_fields):")
    for field, note in EXTRA_FIELD_NOTES.items():
        print(f"  - {field}: {note}")


if __name__ == "__main__":
    main()
