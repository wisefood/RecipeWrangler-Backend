#!/usr/bin/env python3
"""Build the enriched `recipes_v2` Elasticsearch index from Neo4j.

Unlike `index_sources_to_elastic.py` (thin docs: id/title/ingredients/tags),
this indexes every field the recipe search filters and sorts on:
duration, serves, denormalized allergens, expert_recipe, has_profile, scores.

No LLM calls — pure Neo4j -> Elasticsearch bulk copy.

Usage:
    PYTHONPATH=src python scripts/elasticsearch/index_recipes_v2.py            # all sources
    PYTHONPATH=src python scripts/elasticsearch/index_recipes_v2.py --sources FoodHero
    PYTHONPATH=src python scripts/elasticsearch/index_recipes_v2.py --recreate  # drop index first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import fetch_all_recipe_scores

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
DEFAULT_INDEX = "recipes_v2"
DEFAULT_NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
DEFAULT_NEO4J_USERNAME = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER") or "neo4j"
DEFAULT_NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

# Binary curated/non-curated split — mirrors the Neo4j _sort_source CASE:
# FoodHero/HealthyFoods float to the top, every other source shares one bucket.
CURATED_SOURCES = {"foodhero", "healthyfoods"}

INDEX_BODY = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "id": {"type": "keyword"},
            "title": {"type": "text", "fields": {"kw": {"type": "keyword"}}},
            "url": {"type": "keyword", "index": False},
            "image_url": {"type": "keyword", "index": False},
            "source": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "source_rank": {"type": "integer"},
            "ingredients": {"type": "text"},
            "tags": {"type": "keyword"},
            "dish_types": {"type": "keyword"},
            "allergens": {"type": "keyword"},
            "duration": {"type": "float"},
            "serves": {"type": "float"},
            "cost_category": {"type": "keyword"},
            "nutri_score_us": {"type": "keyword"},
            "nutri_color_us": {"type": "keyword"},
            "nutri_score_ie": {"type": "keyword"},
            "nutri_color_ie": {"type": "keyword"},
            "nutri_score_hu": {"type": "keyword"},
            "nutri_color_hu": {"type": "keyword"},
            "nutri_score_eu": {"type": "keyword"},
            "nutri_color_eu": {"type": "keyword"},
            "sust_score": {"type": "float"},
            "expert_recipe": {"type": "boolean"},
            "has_profile": {"type": "boolean"},
            "has_rcsi_nutrition": {"type": "boolean"},
            "has_planeat_nutrition": {"type": "boolean"},
            "nutri_score_planeat": {"type": "keyword"},
            "nutri_color_planeat": {"type": "keyword"},
            "ground_truth_nutrition_source": {"type": "keyword"},
        }
    },
}

QUERY = """
MATCH (r:Recipe)
WHERE ($sources IS NULL OR r.source IN $sources)
  AND coalesce(toString(r.recipe_id), toString(r.id)) IS NOT NULL
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
  RETURN collect(DISTINCT i.name) AS ingredients
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
  RETURN collect(DISTINCT al.name) AS allergens
}
CALL { WITH r
  OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
  RETURN collect(DISTINCT t.name) AS tags,
         collect(DISTINCT CASE WHEN t.category = 'dish-type' THEN t.name END) AS dish_types
}
RETURN
  coalesce(toString(r.recipe_id), toString(r.id)) AS id,
  coalesce(toString(r.title), "") AS title,
  coalesce(toString(r.url), "") AS url,
  coalesce(toString(r.image_url), "") AS image_url,
  coalesce(toString(r.source), "") AS source,
  coalesce(toString(r.source_id), "") AS source_id,
  r.duration AS duration,
  r.serves AS serves,
  coalesce(toString(r.cost_category), "") AS cost_category,
  coalesce(r.expert_recipe, false) AS expert_recipe,
  coalesce(r.has_profile, false) AS has_profile,
  coalesce(r.has_rcsi_lab_nutrition, false) AS has_rcsi_nutrition,
  coalesce(r.has_planeat_nutrition, false) AS has_planeat_nutrition,
  coalesce(toString(r.ground_truth_nutrition_source), "") AS ground_truth_nutrition_source,
  ingredients, allergens, tags, dish_types
ORDER BY id
"""


def _clean_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        item = _clean_str(v).lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_from_neo4j(sources: list[str] | None, uri: str, username: str, password: str) -> list[dict]:
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        with driver.session() as session:
            rows = list(session.run(QUERY, sources=sources))
    finally:
        driver.close()

    recipes: list[dict] = []
    for row in rows:
        source = _clean_str(row["source"])
        recipes.append(
            {
                "id": _clean_str(row["id"]),
                "title": _clean_str(row["title"]),
                "url": _clean_str(row["url"]),
                "image_url": _clean_str(row["image_url"]),
                "source": source,
                "source_id": _clean_str(row["source_id"]),
                "source_rank": 0 if source.lower() in CURATED_SOURCES else 1,
                "ingredients": _clean_list(row["ingredients"]),
                "allergens": _clean_list(row["allergens"]),
                "tags": _clean_list(row["tags"]),
                "dish_types": _clean_list(row["dish_types"]),
                "duration": _to_float(row["duration"]),
                "serves": _to_float(row["serves"]),
                "cost_category": _clean_str(row["cost_category"]) or None,
                # Per-region nutri scores + sustainability filled from Postgres below.
                "nutri_score_us": None, "nutri_color_us": None,
                "nutri_score_ie": None, "nutri_color_ie": None,
                "nutri_score_hu": None, "nutri_color_hu": None,
                "nutri_score_eu": None, "nutri_color_eu": None,
                "sust_score": None,
                "expert_recipe": bool(row["expert_recipe"]),
                "has_profile": bool(row["has_profile"]),
                "has_rcsi_nutrition": bool(row["has_rcsi_nutrition"]),
                "has_planeat_nutrition": bool(row["has_planeat_nutrition"]),
                "ground_truth_nutrition_source": _clean_str(
                    row["ground_truth_nutrition_source"]
                ),
            }
        )
    return recipes


def create_index(es_url: str, index: str, recreate: bool) -> None:
    base = f"{es_url.rstrip('/')}/{index}"
    exists = requests.head(base, timeout=10).status_code == 200
    if exists and recreate:
        requests.delete(base, timeout=30).raise_for_status()
        exists = False
    if not exists:
        resp = requests.put(base, json=INDEX_BODY, timeout=30)
        resp.raise_for_status()
        print(f"Created index '{index}'")
    else:
        print(f"Index '{index}' already exists (use --recreate to rebuild mapping)")


def _iter_bulk_lines(recipes: Iterable[dict], index: str) -> Iterable[str]:
    for recipe in recipes:
        rid = recipe["id"]
        if not rid:
            continue
        yield json.dumps({"index": {"_index": index, "_id": rid}}, ensure_ascii=False)
        yield json.dumps(recipe, ensure_ascii=False)


def bulk_index(recipes: list[dict], es_url: str, index: str, batch_size: int) -> tuple[int, int]:
    lines = list(_iter_bulk_lines(recipes, index))
    if not lines:
        return 0, 0

    bulk_url = f"{es_url.rstrip('/')}/_bulk"
    headers = {"Content-Type": "application/x-ndjson"}
    line_batch = max(2, batch_size * 2)

    total_ok = total_fail = 0
    for start in range(0, len(lines), line_batch):
        body = "\n".join(lines[start : start + line_batch]) + "\n"
        resp = requests.post(bulk_url, headers=headers, data=body.encode("utf-8"), timeout=120)
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            op = item.get("index") or item.get("create") or {}
            if 200 <= int(op.get("status", 500)) < 300:
                total_ok += 1
            else:
                total_fail += 1
                print(f"  failed: {op.get('error')}")
    return total_ok, total_fail


def main() -> None:
    parser = argparse.ArgumentParser(description="Build enriched recipes_v2 ES index from Neo4j.")
    parser.add_argument("--sources", nargs="+", default=None, help="Neo4j r.source values (default: all)")
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--recreate", action="store_true", help="Drop the index before indexing")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--neo4j-uri", default=DEFAULT_NEO4J_URI)
    parser.add_argument("--neo4j-username", default=DEFAULT_NEO4J_USERNAME)
    parser.add_argument("--neo4j-password", default=DEFAULT_NEO4J_PASSWORD)
    args = parser.parse_args()

    if not args.neo4j_password:
        raise ValueError("Missing Neo4j password. Set NEO4J_PASSWORD or pass --neo4j-password.")

    print(f"Fetching from Neo4j: sources={args.sources or 'ALL'}")
    recipes = fetch_from_neo4j(args.sources, args.neo4j_uri, args.neo4j_username, args.neo4j_password)
    print(f"Found {len(recipes)} recipes")
    if not recipes:
        print("Nothing to index.")
        return

    print("Fetching nutri/sustainability scores from Postgres...")
    scores = fetch_all_recipe_scores()
    matched = 0
    for recipe in recipes:
        score = scores.get(recipe["id"])
        if score:
            matched += 1
            for region in ("us", "ie", "hu", "eu"):
                region_score = score.get(region)
                if region_score:
                    recipe[f"nutri_score_{region}"] = region_score["nutri_score"]
                    recipe[f"nutri_color_{region}"] = region_score["nutri_color"]
            recipe["sust_score"] = score["sust_score"]
    print(f"  matched scores for {matched}/{len(recipes)} recipes")

    create_index(args.es_url, args.index, args.recreate)
    ok, fail = bulk_index(recipes, args.es_url, args.index, args.batch_size)
    print(f"Done. indexed={ok} failed={fail}")


if __name__ == "__main__":
    main()
