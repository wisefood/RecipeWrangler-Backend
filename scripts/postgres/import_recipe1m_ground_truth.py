#!/usr/bin/env python3
"""Import Recipe1M ground-truth nutritional profiles into Postgres.

Reads data/processed/recipe1m/recipes_with_nutritional_info.json (51 k recipes)
and upserts one row per recipe with nutrition_source='recipe1m_original'.

Nutritional fields stored per 100g (from nutr_values_per100g):
  energy (kcal), fat, protein, salt, saturates, sugars

Nutri-Score is computed from the per-100g values (fibre approximated as 0,
fruit_percentage=0 since both are absent from this dataset).

Total/per-serving nutrients are also stored by scaling per-100g values by
total ingredient weight (sum of weight_per_ingr).
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

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402
load_runtime_env()

from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values  # noqa: E402

SOURCE_LABEL = "recipe1m"
NUTRITION_SOURCE = "recipe1m_original"
DEFAULT_INPUT = REPO_ROOT / "data" / "processed" / "recipe1m" / "recipes_with_nutritional_info.json"


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _compute_nutri_score(nutr100: dict[str, Any]) -> dict[str, Any] | None:
    """Compute Nutri-Score from per-100g values (fibre=0, fruit_pct=0)."""
    energy_kcal = _to_float(nutr100.get("energy"))
    fat = _to_float(nutr100.get("fat"))
    protein = _to_float(nutr100.get("protein"))
    salt_g = _to_float(nutr100.get("salt"))
    sat_fat = _to_float(nutr100.get("saturates"))
    sugar = _to_float(nutr100.get("sugars"))

    if any(v is None for v in (energy_kcal, fat, protein, salt_g, sat_fat, sugar)):
        return None

    nutrient_values = {
        "energy": energy_kcal * 4.184,   # kcal/100g → kJ/100g
        "sugar": sugar,
        "saturated_fats": sat_fat,
        "sodium": salt_g * 400.0,        # salt g/100g → sodium mg/100g (×400)
        "fibers": 0.0,                   # not present in recipe1m ground truth
        "proteins": protein,
        "fruit_percentage": 0.0,         # not available
    }
    try:
        breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
        breakdown["inputs"] = {
            "source": "recipe1m_original_per100g_fibre0",
            "basis": "per_100g_direct",
            "note": "fibre and fruit_percentage approximated as 0",
        }
        return breakdown
    except Exception:
        return None


def _build_trace(recipe: dict[str, Any]) -> dict[str, Any]:
    return {
        "nutr_values_per100g": recipe.get("nutr_values_per100g"),
        "fsa_lights_per100g": recipe.get("fsa_lights_per100g"),
        "weight_per_ingr": recipe.get("weight_per_ingr"),
        "ingredients": recipe.get("ingredients"),
    }


def _build_totals(recipe: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    """Compute total and per-100g-equivalent nutrient dicts scaled to actual recipe weight."""
    nutr100 = recipe.get("nutr_values_per100g") or {}
    weights = recipe.get("weight_per_ingr") or []
    total_weight_g = sum(float(w) for w in weights if w is not None)
    if total_weight_g <= 0:
        return None, None

    scale = total_weight_g / 100.0

    energy_kcal = _to_float(nutr100.get("energy"))
    fat = _to_float(nutr100.get("fat"))
    protein = _to_float(nutr100.get("protein"))
    salt_g = _to_float(nutr100.get("salt"))
    sat_fat = _to_float(nutr100.get("saturates"))
    sugar = _to_float(nutr100.get("sugars"))

    if any(v is None for v in (energy_kcal, fat, protein, salt_g, sat_fat, sugar)):
        return None, None

    # sodium: salt(g/100g) × (total_weight/100) × 1000 = salt(g) × 10 per recipe?
    # Actually: sodium_mg = salt_g_per100g × 400 (salt→sodium ratio) × scale
    total = {
        "energy_kcal": round(energy_kcal * scale, 4),
        "fat_g": round(fat * scale, 4),
        "saturated_fat_g": round(sat_fat * scale, 4),
        "carbohydrate_g": None,  # not in recipe1m ground truth
        "sugar_g": round(sugar * scale, 4),
        "fibre_g": None,         # not in recipe1m ground truth
        "protein_g": round(protein * scale, 4),
        "sodium_mg": round(salt_g * 400.0 * scale, 4),
    }
    # Remove None values so downstream code handles missing gracefully
    total = {k: v for k, v in total.items() if v is not None}

    serves = _to_float(recipe.get("serves")) or 1.0
    per_serving = {k: round(v / serves, 4) for k, v in total.items()}

    return total, per_serving


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write", action="store_true", help="Persist to Postgres (default: dry-run).")
    args = parser.parse_args()

    print(f"Loading {args.input} ...", flush=True)
    recipes = json.loads(args.input.read_text())
    print(f"Loaded {len(recipes)} recipes.", flush=True)

    if args.limit:
        recipes = recipes[: args.limit]

    ok = failed = skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    bar = tqdm(recipes, desc="Importing recipe1m ground truth", unit="recipe")
    for recipe in bar:
        recipe_id = str(recipe.get("id") or "").strip()
        title = str(recipe.get("title") or "").strip()
        if not recipe_id:
            skipped += 1
            continue

        nutr100 = recipe.get("nutr_values_per100g") or {}
        if not nutr100:
            skipped += 1
            continue

        breakdown = _compute_nutri_score(nutr100)
        nutri_score_payload = None
        if isinstance(breakdown, dict):
            nutri_score_payload = {
                "score": breakdown.get("score"),
                "nutri_score": breakdown.get("nutri_score"),
                "color": breakdown.get("color"),
            }

        total_nutrients, per_serving = _build_totals(recipe)

        record = {
            "recipe_id": recipe_id,
            "title": title,
            "source": SOURCE_LABEL,
            "nutrition_source": NUTRITION_SOURCE,
            "total_nutrients": total_nutrients,
            "total_nutrients_per_serving": per_serving,
            "nutri_score": nutri_score_payload,
            "nutri_score_breakdown": breakdown,
            "nutrition_profiling_details": None,
            "nutrition_profiling_debug": {"source": "recipes_with_nutritional_info.json"},
            "trace": _build_trace(recipe),
            "pipeline_version": "recipe1m_ground_truth_v1",
            "computed_at": now_iso,
        }

        if args.write:
            try:
                upsert_recipe_profiling_trace(record)
                ok += 1
            except Exception as exc:
                print(f"\nERROR {recipe_id}: {exc}", flush=True)
                failed += 1
        else:
            ok += 1

        bar.set_postfix(ok=ok, skipped=skipped, failed=failed)

    print(f"\nDone. ok={ok}  skipped={skipped}  failed={failed}  write_mode={args.write}")


if __name__ == "__main__":
    main()
