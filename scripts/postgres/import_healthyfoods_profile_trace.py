#!/usr/bin/env python3
"""One-off HealthyFoods profiling import into Postgres trace table.

Pipeline:
1) Load data/HealthyFoods/HealthyFood_recipes_clean.json
2) Drop recipes missing duration or serves
3) Ignore notes
4) Map structured fields directly to pipeline state (no LLM parse step)
5) Run Weight_Calculator → Recipe_Profiling_Node
6) Upsert into nutrients-recipe-profiles
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "data" / "HealthyFoods" / "HealthyFood_recipes_clean.json"
DEFAULT_REGION = "US"
DEFAULT_SOURCE_LABEL = "HealthyFoods"
DEFAULT_FAILURES_OUT = REPO_ROOT / "backups" / "healthyfoods_profile_failures.json"


def _as_text(value: object) -> str:
    return str(value or "").strip()


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _has_required_fields(recipe: dict[str, Any]) -> bool:
    return bool(_as_text(recipe.get("duration")) and _as_text(recipe.get("serves")))


def _duration_minutes(value: object) -> float | None:
    """Convert duration field (int minutes or string) to float minutes."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_from_region(region: str) -> str:
    region_norm = (region or DEFAULT_REGION).strip().upper()
    if region_norm == "IE":
        return "irish"
    if region_norm == "US":
        return "usda"
    if region_norm == "HU":
        return "hungarian"
    raise ValueError(f"Unsupported region '{region}'. Supported: IE, US, HU")


def _profile_meta() -> str:
    return os.getenv("NUTRITION_PROFILE_PIPELINE_VERSION", "v1")


def _iter_prepared(
    raw: dict[str, Any],
    limit: int | None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    dropped: list[str] = []
    prepared: list[tuple[str, dict[str, Any]]] = []
    for title_key, payload in raw.items():
        if not isinstance(payload, dict):
            dropped.append(str(title_key))
            continue
        if not _has_required_fields(payload):
            dropped.append(str(title_key))
            continue
        recipe = dict(payload)
        recipe.pop("notes", None)  # explicitly ignored by request
        prepared.append((str(title_key), recipe))

    if limit is not None and limit > 0:
        prepared = prepared[:limit]
    return prepared, dropped


def run_healthyfoods_import(
    input_path: Path,
    region: str = DEFAULT_REGION,
    source_label: str = DEFAULT_SOURCE_LABEL,
    limit: int | None = None,
    dry_run: bool = True,
    failures_out: Path | None = DEFAULT_FAILURES_OUT,
) -> dict[str, Any]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Expected top-level object in HealthyFoods clean JSON.")

    total_rows = len(raw)
    prepared, dropped = _iter_prepared(raw, limit)
    nutrition_source = _source_from_region(region)
    profile_pipeline_version = _profile_meta()

    profiled = 0
    upserted = 0
    failed: list[dict[str, str]] = []

    bar = tqdm(prepared, desc="HealthyFoods profiling", unit="recipe")
    for idx, (title_key, recipe) in enumerate(bar, start=1):
        recipe_id = _as_text(recipe.get("recipe_id")) or _as_text(recipe.get("id"))
        if not recipe_id:
            failed.append(
                {
                    "title": title_key,
                    "recipe_id": "",
                    "error": "Missing recipe_id/id in cleaned HealthyFoods payload.",
                }
            )
            bar.set_postfix(ok=profiled, upserted=upserted, failed=len(failed))
            continue

        title = _as_text(recipe.get("title")) or title_key
        ingredient_lines = _as_list(recipe.get("ingredients"))
        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
        serves = float(recipe.get("serves") or 1)
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
                "region": region,
                "debug": False,
            })
            if not isinstance(profile_result, dict):
                raise ValueError("Profiling pipeline returned non-dict payload.")
        except Exception as exc:
            failed.append({"title": title_key, "recipe_id": recipe_id, "error": str(exc)})
            bar.set_postfix(ok=profiled, upserted=upserted, failed=len(failed))
            continue

        profiled += 1
        nutrition_source_key = profile_result.get("nutrition_source_key") or nutrition_source
        suffix = f"_{nutrition_source_key}"
        totals = profile_result.get("profiling_totals") or {}
        clean_totals = _extract_clean_totals(totals, suffix)
        clean_per_serving = (
            {k: v / serves for k, v in clean_totals.items()}
            if clean_totals and serves else None
        )

        trace_payload = {
            "recipe_id": recipe_id,
            "title": title,
            "source": source_label,
            "nutrition_source": profile_result.get("nutrition_source") or nutrition_source,
            "total_nutrients": clean_totals,
            "total_nutrients_per_serving": clean_per_serving,
            "nutri_score": profile_result.get("nutri_score"),
            "nutri_score_breakdown": profile_result.get("nutri_score_breakdown"),
            "nutrition_profiling_details": profile_result.get("ingredients"),
            "nutrition_profiling_debug": profile_result.get("pipeline_trace"),
            "trace": {
                "healthyfoods_recipe": recipe,
                "profile_result": profile_result,
            },
            "pipeline_version": profile_pipeline_version,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

        if not dry_run:
            upsert_recipe_profiling_trace(trace_payload)
            upserted += 1

        bar.set_postfix(ok=profiled, upserted=upserted, failed=len(failed))
        if idx % 100 == 0:
            tqdm.write(
                f"[progress] processed={idx}/{len(prepared)} ok={profiled} "
                f"upserted={upserted} failed={len(failed)}"
            )

    if failures_out is not None:
        failures_out.parent.mkdir(parents=True, exist_ok=True)
        failures_out.write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return {
        "input_rows": total_rows,
        "dropped_missing_duration_or_serves": len(dropped),
        "dropped_titles_sample": dropped[:20],
        "ready_rows": len(prepared),
        "profiled_rows": profiled,
        "upserted_rows": upserted,
        "failed_rows": len(failed),
        "failures_out": str(failures_out) if failures_out is not None else None,
        "dry_run": dry_run,
        "region": region,
        "nutrition_source": nutrition_source,
        "source_label": source_label,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="HealthyFoods clean JSON path")
    parser.add_argument("--region", default=DEFAULT_REGION, choices=["US", "IE", "HU"])
    parser.add_argument("--source-label", default=DEFAULT_SOURCE_LABEL)
    parser.add_argument("--limit", type=int, default=None, help="Optional cap of recipes to process")
    parser.add_argument(
        "--failures-out",
        type=Path,
        default=DEFAULT_FAILURES_OUT,
        help="Path to write failed recipe entries as JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing to Postgres (default behavior if --write is omitted).",
    )
    parser.add_argument("--write", action="store_true", help="Persist profiling traces into Postgres.")
    args = parser.parse_args()

    dry_run = True
    if args.write:
        dry_run = False
    elif args.dry_run:
        dry_run = True

    result = run_healthyfoods_import(
        input_path=args.input,
        region=args.region,
        source_label=args.source_label,
        limit=args.limit,
        dry_run=dry_run,
        failures_out=args.failures_out,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
