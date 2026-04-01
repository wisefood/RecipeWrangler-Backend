#!/usr/bin/env python3
"""Import MyPlate canonical+nutrition profiling metadata into Postgres trace table."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402


DEFAULT_CANONICAL = REPO_ROOT / "data" / "MyPlate" / "myplate_recipes_with_canonical_ingredients.json"
DEFAULT_NUTRITION = REPO_ROOT / "data" / "MyPlate" / "myplate_recipes_nutrition_usda_mweight.json"


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _load_canonical_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        rid = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        if rid:
            out[rid] = entry
    return out


def _load_nutrition_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rid = _as_id(entry.get("recipe_id")) or _as_id(entry.get("id"))
        if rid:
            out[rid] = entry
    return out


def _merge_profile_details(
    canonical_entry: dict[str, Any] | None,
    nutrition_entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    canonical_rows = []
    if isinstance(canonical_entry, dict):
        raw = canonical_entry.get("canonical_ingredients")
        if isinstance(raw, list):
            canonical_rows = [row for row in raw if isinstance(row, dict)]

    details_rows = []
    if isinstance(nutrition_entry, dict):
        raw = nutrition_entry.get("details")
        if isinstance(raw, list):
            details_rows = [row for row in raw if isinstance(row, dict)]

    canonical_by_name: dict[str, list[dict[str, Any]]] = {}
    for row in canonical_rows:
        canonical_by_name.setdefault(_norm_name(row.get("name")), []).append(row)

    merged: list[dict[str, Any]] = []
    for idx, detail in enumerate(details_rows):
        ingredient_name = _norm_name(detail.get("ingredient"))
        canonical_match = None
        bucket = canonical_by_name.get(ingredient_name) or []
        if bucket:
            canonical_match = bucket.pop(0)
        elif idx < len(canonical_rows):
            canonical_match = canonical_rows[idx]
        merged.append(
            {
                "ingredient": detail.get("ingredient"),
                "measurement_raw": (canonical_match or {}).get("measurement"),
                "parsed_quantity": (canonical_match or {}).get("parsed_quantity"),
                "parsed_unit": (canonical_match or {}).get("parsed_unit"),
                "weight_g": detail.get("weight_g", (canonical_match or {}).get("weight_grams")),
                "weight_source": (canonical_match or {}).get("source"),
                "weight_match": (canonical_match or {}).get("match"),
                "matched_nutritional_ingredient": detail.get("matched_nutritional_ingredient"),
                "nutrition_source": detail.get("source_nutrition") or detail.get("source"),
                "nutrition_match_source": detail.get("match_source"),
                "canonical_food_id": detail.get("canonical_food_id"),
                "similarity": detail.get("similarity"),
            }
        )
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--nutrition", type=Path, default=DEFAULT_NUTRITION)
    parser.add_argument(
        "--pipeline-version",
        default=os.getenv("NUTRITION_PROFILE_PIPELINE_VERSION", "myplate_export_v1"),
    )
    parser.add_argument(
        "--mapping-version",
        default=os.getenv("NUTRITION_PROFILE_MAPPING_VERSION", "myplate_canonical_v1"),
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("NUTRITION_PROFILE_EMBEDDING_MODEL", "usda_embedding"),
    )
    parser.add_argument(
        "--ruleset-version",
        default=os.getenv("NUTRITION_PROFILE_RULESET_VERSION", "myplate_rules_v1"),
    )
    args = parser.parse_args()

    canonical_index = _load_canonical_index(args.canonical)
    nutrition_index = _load_nutrition_index(args.nutrition)
    all_ids = sorted(set(canonical_index.keys()) | set(nutrition_index.keys()))

    inserted = 0
    skipped = 0
    for rid in all_ids:
        canonical_entry = canonical_index.get(rid)
        nutrition_entry = nutrition_index.get(rid)
        if not isinstance(canonical_entry, dict) and not isinstance(nutrition_entry, dict):
            skipped += 1
            continue

        title = None
        source = "myplate"
        if isinstance(canonical_entry, dict):
            title = canonical_entry.get("title") or title
            source = str(canonical_entry.get("source") or source)
        if isinstance(nutrition_entry, dict):
            title = nutrition_entry.get("title") or title
            source = str(nutrition_entry.get("source") or source)

        details = _merge_profile_details(canonical_entry, nutrition_entry)
        debug_payload = nutrition_entry.get("debug") if isinstance(nutrition_entry, dict) else None
        total_nutrients = (
            nutrition_entry.get("totals_usda")
            if isinstance(nutrition_entry, dict)
            else None
        )
        total_nutrients_per_serving = (
            nutrition_entry.get("totals_per_serving_usda")
            if isinstance(nutrition_entry, dict)
            else None
        )
        nutri_score = nutrition_entry.get("nutri_score") if isinstance(nutrition_entry, dict) else None

        upsert_recipe_profiling_trace(
            {
                "recipe_id": rid,
                "title": title,
                "source": source,
                "nutrition_source": "usda",
                "total_nutrients": total_nutrients,
                "total_nutrients_per_serving": total_nutrients_per_serving,
                "nutri_score": nutri_score,
                "nutri_score_breakdown": None,
                "nutrition_profiling_details": details,
                "nutrition_profiling_debug": debug_payload if isinstance(debug_payload, dict) else None,
                "trace": {
                    "canonical_entry": canonical_entry,
                    "nutrition_entry": nutrition_entry,
                },
                "pipeline_version": args.pipeline_version,
                "mapping_version": args.mapping_version,
                "embedding_model": args.embedding_model,
                "ruleset_version": args.ruleset_version,
                "computed_at": None,
            }
        )
        inserted += 1

    print(f"rows_total={len(all_ids)}")
    print(f"rows_upserted={inserted}")
    print(f"rows_skipped={skipped}")


if __name__ == "__main__":
    main()
