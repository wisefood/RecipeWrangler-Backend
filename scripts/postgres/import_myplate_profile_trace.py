#!/usr/bin/env python3
"""Run MyPlate recipes through the profiling pipeline and store results in Postgres.

MyPlate recipes already exist in Neo4j — this script only writes to Postgres.
No Neo4j writes are performed so there is no risk of duplicate Recipe nodes.

Pipeline per recipe:
1) Split ingredient lines → ingredient_names + measurements
2) Run Weight_Calculator → Recipe_Profiling_Node (skip-parse, region=US)
3) Upsert into nutrients-recipe-profiles with nutrition_source=usda

Usage:
    # Dry-run (no writes):
    python3 import_myplate_profile_trace.py

    # Persist to Postgres:
    python3 import_myplate_profile_trace.py --write

    # Test with first 20 recipes:
    python3 import_myplate_profile_trace.py --write --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_API_KEY"] = ""
os.environ["LANGSMITH_API_KEY"] = ""

from tqdm import tqdm  # noqa: E402

from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "data" / "MyPlate" / "myplate_recipes_clean.json"
REGION = "US"
NUTRITION_SOURCE = "usda"
SOURCE_LABEL = "MyPlate"
DEFAULT_FAILURES_OUT = REPO_ROOT / "backups" / "myplate_profile_failures.json"


def _as_text(value: object) -> str:
    return str(value or "").strip()


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _duration_minutes(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist results to Postgres.")
    parser.add_argument("--limit", type=int, default=None, help="Max recipes to process.")
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Path to MyPlate clean JSON (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--failures-out",
        type=Path,
        default=DEFAULT_FAILURES_OUT,
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Skip recipes that already have a usda nutrition row in Postgres.",
    )
    args = parser.parse_args()

    raw: dict[str, Any] = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("Expected top-level object in MyPlate clean JSON.")

    recipes = list(raw.items())

    if args.only_missing:
        import psycopg2
        from recipe_wrangler.utils.nutrition_postgres import _get_config
        cfg = _get_config()
        conn = psycopg2.connect(host=cfg["db_host"], port=cfg["db_port"], dbname=cfg["db_name"],
                                user=cfg["db_user"], password=cfg["db_password"])
        cur = conn.cursor()
        cur.execute("""
            SELECT recipe_id FROM "nutrients-recipe-profiles"
            WHERE source = 'MyPlate' AND nutrition_source = 'usda'
            AND total_nutrients_per_serving IS NOT NULL AND total_nutrients_per_serving::text != 'null'
        """)
        existing = {r[0] for r in cur.fetchall()}
        conn.close()
        before = len(recipes)
        recipes = [(k, v) for k, v in recipes if isinstance(v, dict) and
                   str(v.get("recipe_id") or v.get("id") or "") not in existing]
        print(f"[only-missing] {before} total → {len(recipes)} need profiling ({before - len(recipes)} already done)")

    if args.limit:
        recipes = recipes[: args.limit]

    pipeline_version = os.getenv("NUTRITION_PROFILE_PIPELINE_VERSION", "v1")
    profiled = 0
    upserted = 0
    failed: list[dict[str, str]] = []

    bar = tqdm(recipes, desc="MyPlate profiling", unit="recipe")
    for idx, (title_key, recipe) in enumerate(bar, start=1):
        if not isinstance(recipe, dict):
            failed.append({"title": title_key, "recipe_id": "", "error": "not a dict"})
            continue

        recipe_id = _as_text(recipe.get("recipe_id") or recipe.get("id"))
        if not recipe_id:
            failed.append({"title": title_key, "recipe_id": "", "error": "missing recipe_id"})
            continue

        title = _as_text(recipe.get("title")) or title_key
        ingredient_lines = _as_list(recipe.get("ingredients"))
        if not ingredient_lines:
            failed.append({"title": title_key, "recipe_id": recipe_id, "error": "no ingredients"})
            continue

        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
        serves = float(recipe.get("serves") or 4)
        total_time = _duration_minutes(recipe.get("duration"))
        directions = _as_list(recipe.get("instructions"))

        try:
            profile_result = Recipe_Profiling_Chain_Structured.invoke({
                "title": title,
                "ingredient_names": ingredient_names,
                "measurements": measurements,
                "serves": serves,
                "total_time": total_time,
                "directions": directions,
                "region": REGION,
                "debug": False,
            })
            if not isinstance(profile_result, dict):
                raise ValueError("pipeline returned non-dict")
        except Exception as exc:
            failed.append({"title": title_key, "recipe_id": recipe_id, "error": str(exc)})
            bar.set_postfix(ok=profiled, upserted=upserted, failed=len(failed))
            continue

        profiled += 1
        nutrition_source_key = profile_result.get("nutrition_source_key") or NUTRITION_SOURCE
        suffix = f"_{nutrition_source_key}"
        totals = profile_result.get("profiling_totals") or {}
        clean_totals = _extract_clean_totals(totals, suffix)
        clean_per_serving = (
            {k: v / serves for k, v in clean_totals.items()}
            if clean_totals and serves
            else None
        )

        trace_payload = {
            "recipe_id": recipe_id,
            "title": title,
            "source": SOURCE_LABEL,
            "nutrition_source": profile_result.get("nutrition_source") or NUTRITION_SOURCE,
            "total_nutrients": clean_totals,
            "total_nutrients_per_serving": clean_per_serving,
            "nutri_score": profile_result.get("nutri_score"),
            "nutri_score_breakdown": profile_result.get("nutri_score_breakdown"),
            "nutrition_profiling_details": profile_result.get("ingredients"),
            "nutrition_profiling_debug": profile_result.get("pipeline_trace"),
            "trace": {
                "myplate_recipe": recipe,
                "profile_result": profile_result,
            },
            "pipeline_version": pipeline_version,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

        if args.write:
            upsert_recipe_profiling_trace(trace_payload)
            upserted += 1

        bar.set_postfix(ok=profiled, upserted=upserted, failed=len(failed))
        if idx % 100 == 0:
            tqdm.write(
                f"[progress] processed={idx}/{len(recipes)} ok={profiled} "
                f"upserted={upserted} failed={len(failed)}"
            )

    if args.failures_out:
        args.failures_out.parent.mkdir(parents=True, exist_ok=True)
        args.failures_out.write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    result = {
        "input_rows": len(raw),
        "ready_rows": len(recipes),
        "profiled_rows": profiled,
        "upserted_rows": upserted,
        "failed_rows": len(failed),
        "failures_out": str(args.failures_out),
        "dry_run": not args.write,
        "region": REGION,
        "nutrition_source": NUTRITION_SOURCE,
        "source_label": SOURCE_LABEL,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
