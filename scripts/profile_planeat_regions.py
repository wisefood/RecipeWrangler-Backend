#!/usr/bin/env python3
"""Run regional nutrition profiling for PLANEAT recipes.

PLANEAT recipes (data/ESSRG/ESSRG_recipes_clean.json) already carry resolved
CoFID ingredient names and gram weights, so no LLM parsing or weight estimation
is needed. For each recipe and each region this script:

  1. calls nutritional_tool_chroma (Chroma match -> Postgres per-100g -> scale
     -> aggregate),
  2. computes a per-100g Nutri-Score,
  3. upserts a profiling trace row into Postgres
     (source="PLANEAT", nutrition_source=region),
  4. patches the recipe's recipes_v2 Elasticsearch document with the per-region
     Nutri-Score grade and color.

Dry-run by default. Pass --write to enable Postgres + Elasticsearch writes.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recipe_wrangler.api.main import get_settings  # noqa: E402
from recipe_wrangler.tools.nutritional_calculator import (  # noqa: E402
    nutritional_tool_chroma,
)
from recipe_wrangler.utils.nutri_score import (  # noqa: E402
    compute_nutri_score_breakdown_from_values,
)
from recipe_wrangler.utils.nutrition_postgres import (  # noqa: E402
    upsert_recipe_profiling_trace,
)

REGIONS = ["usda", "irish", "hungarian", "eu"]
ES_REGION_CODE = {"usda": "us", "irish": "ie", "hungarian": "hu", "eu": "eu"}

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "ESSRG" / "ESSRG_recipes_clean.json"
CHECKPOINT_PATH = Path(__file__).resolve().parent / "profile_planeat_regions.checkpoint.json"


def _compute_nutri_score_per100g(totals: dict, total_weight_g: float) -> dict | None:
    if total_weight_g <= 0:
        return None
    scale = 100.0 / total_weight_g
    inputs = {
        "energy": totals["energy_kcal"] * scale * 4.184,  # kJ per 100g
        "sugar": totals["sugar_g"] * scale,
        "saturated_fats": totals["saturated_fat_g"] * scale,
        "sodium": totals["sodium_mg"] * scale / 1000,  # g per 100g
        "fibers": totals["fibre_g"] * scale,
        "proteins": totals["protein_g"] * scale,
        "fruit_percentage": 0,
    }
    breakdown = compute_nutri_score_breakdown_from_values(inputs)
    breakdown["basis"] = "per_100g_from_weight"
    return breakdown


def _load_recipes() -> list[dict]:
    with open(DATA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_checkpoint(resume: bool) -> set[str]:
    if not resume or not CHECKPOINT_PATH.exists():
        return set()
    try:
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def _save_checkpoint(done: set[str]) -> None:
    tmp = CHECKPOINT_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(done), fh, indent=2)
    os.replace(tmp, CHECKPOINT_PATH)


def _patch_elasticsearch(settings, recipe_id: str, region: str, breakdown: dict) -> None:
    code = ES_REGION_CODE[region]
    requests.post(
        f"{settings.elastic_url}/recipes_v2/_update/{recipe_id}",
        json={
            "doc": {
                f"nutri_score_{code}": breakdown.get("nutri_score"),
                f"nutri_color_{code}": breakdown.get("color"),
            }
        },
        timeout=5,
    ).raise_for_status()


def _process_region(rec: dict, region: str, settings, write: bool) -> str:
    """Profile one recipe for one region. Returns a short status string."""
    recipe_id = rec["recipe_id"]
    title = rec["title"]
    serves = float(rec.get("serves") or 1)
    details_in = rec.get("ingredient_details") or []

    if not details_in:
        return "skip-no-ingredients"

    details_with_weight = [d for d in details_in if d.get("weight_g") is not None]
    if not details_with_weight:
        return "skip-no-ingredients"
    names = [d["name"] for d in details_with_weight]
    weights = [float(d["weight_g"]) for d in details_with_weight]
    total_weight_g = float(sum(weights))

    result = nutritional_tool_chroma.invoke({
        "title": title,
        "ingredient_names": names,
        "weights": weights,
        "source": region,
        "serves": serves,
    })
    totals = result["clean_totals"]
    per_serving_totals = {
        k: (float(v) / serves if serves else None) for k, v in totals.items()
    }

    breakdown = _compute_nutri_score_per100g(totals, total_weight_g)

    if not write:
        grade = breakdown.get("nutri_score") if breakdown else None
        return f"dry-run grade={grade}"

    upsert_recipe_profiling_trace(
        {
            "recipe_id": recipe_id,
            "title": title,
            "source": "PLANEAT",
            "nutrition_source": region,
            "pipeline_version": "cofid_direct_weight_known",
            "total_nutrients": totals,
            "total_nutrients_per_serving": per_serving_totals,
            "nutri_score": {
                "nutri_score": breakdown.get("nutri_score"),
                "color": breakdown.get("color"),
                "score": breakdown.get("score"),
            }
            if breakdown
            else None,
            "nutri_score_breakdown": breakdown,
            "nutrition_profiling_details": result["details"],
            "nutrition_profiling_debug": {
                "method": "cofid_direct_weight_known",
                "region": region,
                "ingredient_count": len(names),
                "total_weight_g": total_weight_g,
            },
            "trace": {
                "serves": serves,
                "serves_source": rec.get("serves_source", "llm_estimate"),
                "total_weight_g": total_weight_g,
            },
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    if breakdown:
        _patch_elasticsearch(settings, recipe_id, region, breakdown)

    grade = breakdown.get("nutri_score") if breakdown else None
    return f"written grade={grade}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Regional profiling for PLANEAT recipes.")
    parser.add_argument("--write", action="store_true", help="enable DB + ES writes (default: dry-run)")
    parser.add_argument("--no-resume", action="store_true", help="ignore checkpoint, re-process all")
    parser.add_argument("--limit", type=int, default=None, help="stop after N recipes")
    parser.add_argument("--regions", type=str, default=None, help="comma-separated subset, e.g. usda,irish")
    args = parser.parse_args()

    regions = REGIONS
    if args.regions:
        requested = [r.strip() for r in args.regions.split(",") if r.strip()]
        unknown = [r for r in requested if r not in REGIONS]
        if unknown:
            parser.error(f"unknown region(s): {unknown}; valid: {REGIONS}")
        regions = [r for r in REGIONS if r in requested]

    settings = get_settings()
    recipes = _load_recipes()
    if args.limit is not None:
        recipes = recipes[: args.limit]

    done = _load_checkpoint(resume=not args.no_resume)
    mode = "WRITE" if args.write else "DRY-RUN"
    print(f"[{mode}] {len(recipes)} recipes x {regions} | resume={len(done)} done")

    processed = 0
    for rec in recipes:
        recipe_id = rec["recipe_id"]
        for region in regions:
            key = f"{recipe_id}:{region}"
            if key in done:
                continue
            try:
                status = _process_region(rec, region, settings, args.write)
                print(f"  {key}: {status}")
            except Exception as exc:  # one failure must not abort the run
                print(f"  {key}: ERROR {type(exc).__name__}: {exc}")
                continue
            if args.write and status != "skip-no-ingredients":
                done.add(key)
                _save_checkpoint(done)
            processed += 1

    print(f"Done. processed={processed} write={args.write}")


if __name__ == "__main__":
    main()
