#!/usr/bin/env python3
"""One-off script: sample 1000 random recipe1m recipes with verified image URLs,
profile them for all 3 regions (IE, HU, US), and upsert into nutrients-recipe-profiles.

Pipeline:
1) Pull a random oversample from Neo4j (default 5000) to account for dead URLs
2) HEAD-check each image URL concurrently; keep first 1000 that return 200
3) For each region: run Weight_Calculator → Recipe_Profiling_Node
4) Upsert traces into Postgres
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_API_KEY"] = ""
os.environ["LANGSMITH_API_KEY"] = ""

from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain_Structured  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from recipe_wrangler.utils.neo4j_utils import run_query  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402

REGIONS = ["IE", "HU", "US"]
REGION_TO_SOURCE = {"IE": "irish", "HU": "hungarian", "US": "usda"}
TARGET = 1000
OVERSAMPLE = 5000
IMAGE_TIMEOUT = 5
IMAGE_WORKERS = 50
SOURCE_LABEL = "recipe1m"
LAYER1_PATH = REPO_ROOT / "data" / "raw" / "recipe1m" / "layer1.json"
FAILURES_OUT = REPO_ROOT / "backups" / "recipe1m_subset_failures.json"


def _profile_meta() -> str:
    return os.getenv("NUTRITION_PROFILE_PIPELINE_VERSION", "v1")


def load_layer1_index() -> dict[str, list[str]]:
    """Load layer1.json and return {recipe_id: [ingredient_text, ...]}."""
    print("Loading layer1.json ingredient index...")
    with open(LAYER1_PATH, encoding="utf-8") as f:
        data = json.load(f)
    index: dict[str, list[str]] = {}
    for entry in data:
        rid = entry.get("id", "")
        ingredients = [ing["text"] for ing in entry.get("ingredients", []) if ing.get("text")]
        if rid and ingredients:
            index[rid] = ingredients
    print(f"  Loaded {len(index)} recipes with ingredients")
    return index


def fetch_random_candidates(n: int) -> list[dict]:
    """Pull n random recipe1m recipes from Neo4j."""
    rows = run_query(
        """
        MATCH (r:Recipe)
        WHERE toLower(coalesce(r.source, '')) = 'recipe1m'
          AND r.image_url IS NOT NULL AND r.image_url <> ''
        WITH r ORDER BY rand() LIMIT $n
        RETURN
            coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
            r.title AS title,
            r.image_url AS image_url,
            r.instructions AS instructions
        """,
        {"n": n},
    )
    return [dict(r) for r in rows]


def check_image(recipe: dict) -> tuple[dict, bool]:
    try:
        r = requests.head(recipe["image_url"], timeout=IMAGE_TIMEOUT, allow_redirects=True)
        return recipe, r.status_code == 200
    except Exception:
        return recipe, False


def filter_valid_images(candidates: list[dict], target: int) -> list[dict]:
    valid: list[dict] = []
    print(f"Checking {len(candidates)} image URLs with {IMAGE_WORKERS} workers...")
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        futures = {pool.submit(check_image, r): r for r in candidates}
        bar = tqdm(as_completed(futures), total=len(candidates), desc="Image check", unit="url")
        for future in bar:
            recipe, ok = future.result()
            if ok:
                valid.append(recipe)
            bar.set_postfix(valid=len(valid), needed=target)
            if len(valid) >= target:
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                break
    return valid[:target]


def run_import(dry_run: bool = True) -> dict:
    layer1 = load_layer1_index()

    print(f"[1/3] Sampling {OVERSAMPLE} random recipe1m recipes from Neo4j...")
    candidates = fetch_random_candidates(OVERSAMPLE)
    # Only keep recipes that have ingredients in layer1
    candidates = [r for r in candidates if r["recipe_id"] in layer1]
    print(f"      Got {len(candidates)} candidates with ingredients")

    print(f"[2/3] Filtering to {TARGET} with working image URLs...")
    valid = filter_valid_images(candidates, TARGET)
    print(f"      {len(valid)} recipes with valid images")

    if len(valid) < TARGET:
        print(f"      WARNING: only found {len(valid)} valid images (target was {TARGET})")

    pipeline_version = _profile_meta()
    total_upserted = 0
    total_failed = 0
    failures: list[dict] = []

    print(f"[3/3] Profiling {len(valid)} recipes × {len(REGIONS)} regions...")
    for region in REGIONS:
        nutrition_source = REGION_TO_SOURCE[region]
        bar = tqdm(valid, desc=f"Profiling [{region}]", unit="recipe")
        for recipe in bar:
            recipe_id = recipe["recipe_id"]
            title = recipe["title"]
            ingredient_lines = layer1.get(recipe_id, [])
            instructions = recipe.get("instructions") or []
            if isinstance(instructions, str):
                instructions = [instructions]

            from recipe_wrangler.tools.recipe_profiling_chain import split_ingredient_lines
            ingredient_names, measurements = split_ingredient_lines(ingredient_lines)

            try:
                result = Recipe_Profiling_Chain_Structured.invoke({
                    "title": title,
                    "ingredient_names": ingredient_names,
                    "measurements": measurements,
                    "serves": 4,
                    "total_time": None,
                    "directions": instructions,
                    "region": region,
                    "debug": False,
                })
                if not isinstance(result, dict):
                    raise ValueError("Non-dict result from pipeline")
            except Exception as exc:
                failures.append({"recipe_id": recipe_id, "title": title, "region": region, "error": str(exc)})
                total_failed += 1
                bar.set_postfix(ok=total_upserted, failed=total_failed)
                continue

            serves = 4
            ns_key = result.get("nutrition_source_key") or nutrition_source
            suffix = f"_{ns_key}"
            totals = result.get("profiling_totals") or {}
            clean_totals = _extract_clean_totals(totals, suffix)
            clean_per_serving = (
                {k: v / serves for k, v in clean_totals.items()}
                if clean_totals else None
            )

            trace_payload = {
                "recipe_id": recipe_id,
                "title": title,
                "source": SOURCE_LABEL,
                "nutrition_source": result.get("nutrition_source") or nutrition_source,
                "total_nutrients": clean_totals,
                "total_nutrients_per_serving": clean_per_serving,
                "nutri_score": result.get("nutri_score"),
                "nutri_score_breakdown": result.get("nutri_score_breakdown"),
                "nutrition_profiling_details": result.get("ingredients"),
                "nutrition_profiling_debug": result.get("pipeline_trace"),
                "trace": {"ingredient_lines": ingredient_lines, "profile_result": result},
                "pipeline_version": pipeline_version,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            if not dry_run:
                upsert_recipe_profiling_trace(trace_payload)
                total_upserted += 1

            bar.set_postfix(ok=total_upserted, failed=total_failed)

    FAILURES_OUT.parent.mkdir(parents=True, exist_ok=True)
    FAILURES_OUT.write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n")

    return {
        "candidates_sampled": len(candidates),
        "valid_images": len(valid),
        "regions": REGIONS,
        "total_upserted": total_upserted,
        "total_failed": total_failed,
        "failures_out": str(FAILURES_OUT),
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist to Postgres (default: dry-run)")
    args = parser.parse_args()

    result = run_import(dry_run=not args.write)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
