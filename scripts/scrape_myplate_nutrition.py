#!/usr/bin/env python3
"""Scrape reference nutrition from myplate.food recipe pages via JSON-LD.

Fetches each MyPlate recipe URL from Neo4j, extracts the Recipe JSON-LD block,
and saves structured nutrition to data/MyPlate/myplate_nutrition.json.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
from neo4j import GraphDatabase

NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

OUT_FILE = Path("data/MyPlate/myplate_nutrition.json")
DELAY    = 0.5   # seconds between requests

NUTRIENT_FIELDS = {
    "calories":            "energy_kcal",
    "fatContent":          "fat_g",
    "saturatedFatContent": "saturated_fat_g",
    "cholesterolContent":  "cholesterol_mg",
    "sodiumContent":       "sodium_mg",
    "carbohydrateContent": "carbs_g",
    "fiberContent":        "fibre_g",
    "sugarContent":        "sugar_g",
    "proteinContent":      "protein_g",
}


def parse_value(raw: str) -> float | None:
    """Extract numeric part from strings like '132 calories', '3.41 g'."""
    m = re.search(r"[\d.]+", str(raw))
    return float(m.group()) if m else None


def fetch_nutrition(url: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}")
        return None

    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        r.text, re.DOTALL
    )
    for block in blocks:
        try:
            data = json.loads(block)
        except Exception:
            continue
        if data.get("@type") != "Recipe":
            continue
        nutrition = data.get("nutrition", {})
        if not nutrition:
            return None
        result: dict = {}
        for ld_key, our_key in NUTRIENT_FIELDS.items():
            val = parse_value(nutrition.get(ld_key, ""))
            if val is not None:
                result[our_key] = val
        result["serving_size"] = nutrition.get("servingSize")
        result["recipe_yield"] = data.get("recipeYield")
        return result or None
    return None


def get_recipes(driver) -> list[dict]:
    with driver.session() as s:
        result = s.run(
            "MATCH (r:Recipe {source: 'MyPlate'}) RETURN r.recipe_id AS id, r.url AS url"
        )
        return [{"recipe_id": row["id"], "url": row["url"]} for row in result]


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing to allow resume
    existing: dict[str, dict] = {}
    if OUT_FILE.exists():
        existing = json.loads(OUT_FILE.read_text())
        print(f"Resuming: {len(existing)} already scraped")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    recipes = get_recipes(driver)
    driver.close()
    print(f"{len(recipes)} MyPlate recipes to check")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; research-scraper/1.0)"

    todo = [r for r in recipes if r["recipe_id"] not in existing]
    print(f"{len(todo)} to scrape")

    for i, rec in enumerate(todo):
        nutrition = fetch_nutrition(rec["url"], session)
        existing[rec["recipe_id"]] = {
            "url": rec["url"],
            "nutrition": nutrition,
        }
        if nutrition:
            print(f"  [{i+1}/{len(todo)}] ✓ {rec['url'].split('/')[-1]}")
        else:
            print(f"  [{i+1}/{len(todo)}] ✗ {rec['url'].split('/')[-1]} — no nutrition found")

        if i % 50 == 0:
            OUT_FILE.write_text(json.dumps(existing, indent=2))

        time.sleep(DELAY)

    OUT_FILE.write_text(json.dumps(existing, indent=2))
    with_nutrition = sum(1 for v in existing.values() if v.get("nutrition"))
    print(f"\nDone: {with_nutrition}/{len(existing)} with nutrition → {OUT_FILE}")


if __name__ == "__main__":
    main()
