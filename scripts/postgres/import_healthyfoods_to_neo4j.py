#!/usr/bin/env python3
"""Import HealthyFoods recipes from Postgres traces into Neo4j.

Reads profiling traces stored in Postgres (nutrients-recipe-profiles) and
upserts Recipe nodes + ingredient/allergen/tag graph into Neo4j for any
recipe_id not already present in Neo4j.

Run dry-run first (default), then pass --write to persist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_postgres import get_connection, _get_config
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.repositories.neo4j_recipes import (
    upsert_recipe_to_neo4j,
    detect_allergens_from_names,
    infer_diet_tags,
)


def _as_dict(v: Any) -> dict | None:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _as_list(v: Any) -> list | None:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else None
        except Exception:
            return None
    return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_missing_ids(pg_ids: list[str]) -> list[str]:
    """Return subset of pg_ids not found in Neo4j."""
    found: set[str] = set()
    for i in range(0, len(pg_ids), 500):
        batch = pg_ids[i : i + 500]
        results = run_query(
            "UNWIND $ids AS rid MATCH (r:Recipe) WHERE r.recipe_id = rid RETURN rid",
            {"ids": batch},
        )
        found.update(str(r.get("rid")) for r in results)
    return [rid for rid in pg_ids if rid not in found]


def _extract_recipe_fields(
    recipe_id: str,
    trace: dict,
) -> dict | None:
    """Extract Neo4j-ready fields from a Postgres trace dict."""
    hf = _as_dict(trace.get("healthyfoods_recipe"))
    if not hf:
        return None

    title = str(hf.get("title") or "").strip()
    if not title:
        return None

    serves = _to_float(hf.get("serves")) or 4.0
    duration = _to_float(hf.get("duration")) or 0.0
    image_url = hf.get("image_url") or None
    raw_instructions = _as_list(hf.get("instructions")) or []
    instructions = [str(s) for s in raw_instructions if s]

    raw_ingredients = _as_list(hf.get("ingredients")) or []
    ingredient_lines = [str(s) for s in raw_ingredients if s]

    # Prefer parsed names/measurements from profile_result
    pr = _as_dict(trace.get("profile_result")) or {}
    ingredient_names = _as_list(pr.get("ingredient_names")) or []
    measurements = _as_list(pr.get("measurements")) or []

    # Fall back: derive names from detailed ingredient dicts
    if not ingredient_names:
        ing_dicts = _as_list(pr.get("ingredients")) or []
        ingredient_names = [str(d.get("name") or "") for d in ing_dicts if isinstance(d, dict)]
        measurements = [str(d.get("measurement") or "") for d in ing_dicts if isinstance(d, dict)]

    # Last resort: use raw lines as names
    if not ingredient_names:
        ingredient_names = ingredient_lines[:]
        measurements = [""] * len(ingredient_lines)

    # Align lengths
    n = min(len(ingredient_lines), len(ingredient_names), len(measurements))
    if n == 0:
        return None
    ingredient_lines = ingredient_lines[:n]
    ingredient_names = ingredient_names[:n]
    measurements = measurements[:n]

    # Allergens + diet tags
    raw_allergens = _as_list(pr.get("allergens")) or []
    allergens = list({str(a) for a in raw_allergens if a})
    if not allergens:
        allergens = detect_allergens_from_names(ingredient_names)
    tags = _as_list(pr.get("tag_list")) or _as_list(pr.get("tags")) or []
    tags = list({str(t) for t in tags if t})
    inferred = infer_diet_tags(set(allergens))
    tags = list(set(tags) | set(inferred))

    return {
        "recipe_id": recipe_id,
        "title": title,
        "ingredient_lines": ingredient_lines,
        "ingredient_names": ingredient_names,
        "measurements": measurements,
        "instructions": instructions,
        "duration": duration,
        "serves": serves,
        "image_url": image_url,
        "allergens": allergens,
        "tags": tags,
        "source": "HealthyFoods",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Actually write to Neo4j.")
    parser.add_argument("--limit", type=int, default=None, help="Max recipes to process.")
    args = parser.parse_args()

    cfg = _get_config()
    table = f"\"{cfg['schema']}\".\"{cfg['profiles_table']}\""

    # Get all Postgres recipe IDs from HealthyFoods traces
    with get_connection() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT ON (recipe_id) recipe_id, trace
                FROM {table}
                WHERE trace->>'healthyfoods_recipe' IS NOT NULL
                ORDER BY recipe_id
                """
            )
        ).mappings().all()

    pg_ids = [str(r["recipe_id"]) for r in rows]
    print(f"healthyfoods recipes in postgres: {len(pg_ids)}", flush=True)

    missing = _get_missing_ids(pg_ids)
    print(f"missing from neo4j: {len(missing)}", flush=True)

    if args.limit:
        missing = missing[: args.limit]
        print(f"limited to: {len(missing)}", flush=True)

    # Build lookup dict: recipe_id -> trace
    trace_map = {str(r["recipe_id"]): r["trace"] for r in rows}

    ok = 0
    skipped = 0
    failed = 0

    for idx, recipe_id in enumerate(missing):
        if idx % 100 == 0:
            print(f"  {idx}/{len(missing)} ok={ok} skipped={skipped} failed={failed}", flush=True)

        raw_trace = trace_map.get(recipe_id)
        trace = _as_dict(raw_trace)
        if not trace:
            skipped += 1
            continue

        fields = _extract_recipe_fields(recipe_id, trace)
        if not fields:
            skipped += 1
            continue

        if not args.write:
            ok += 1
            continue

        try:
            upsert_recipe_to_neo4j(**fields)
            ok += 1
        except Exception as e:
            print(f"  ERROR {recipe_id}: {e}", file=sys.stderr, flush=True)
            failed += 1

    print(f"\ntotal_missing={len(missing)}", flush=True)
    print(f"ok={ok}", flush=True)
    print(f"skipped={skipped}", flush=True)
    print(f"failed={failed}", flush=True)
    print(f"write_mode={args.write}", flush=True)


if __name__ == "__main__":
    main()
