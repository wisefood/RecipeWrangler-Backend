#!/usr/bin/env python3
"""Import PLANEAT (ESSRG T442) recipes into Neo4j, PostgreSQL, and Elasticsearch.

No LLM calls — nutrition comes from CoFID-derived values already in the JSON.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/import_planeat.py          # dry run
    PYTHONPATH=src .venv/bin/python scripts/import_planeat.py --write  # commit to DBs
    PYTHONPATH=src .venv/bin/python scripts/import_planeat.py --write --limit 3
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

import os
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import requests
from recipe_wrangler.api.config import get_settings
from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergens_from_names,
    driver as neo4j_driver,
    upsert_recipe_to_neo4j,
)
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values

SOURCE = "PLANEAT"
NUTRITION_SOURCE = "planeat"
PIPELINE_VERSION = "cofid_direct"
RECIPES_FILE = REPO_ROOT / "data" / "ESSRG" / "ESSRG_recipes_clean.json"
CHECKPOINT_FILE = REPO_ROOT / "scripts" / "import_planeat.checkpoint.json"


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> set[str]:
    if CHECKPOINT_FILE.exists():
        return set(json.loads(CHECKPOINT_FILE.read_text()))
    return set()


def save_checkpoint(done: set[str]) -> None:
    CHECKPOINT_FILE.write_text(json.dumps(sorted(done)))


# ---------------------------------------------------------------------------
# Nutri-Score
# ---------------------------------------------------------------------------

def _compute_nutri_score(nutrition: dict) -> dict | None:
    """Compute Nutri-Score breakdown from per-serving nutrition values.

    The nutri_score function requires per-100g values. PLANEAT recipes all have
    serves=1 (so per-serving == per-recipe totals). We scale by total_weight_g
    to get per-100g; if total_weight_g is missing we fall back to per-serving as
    a rough approximation (flagged in breakdown inputs).
    """
    try:
        kcal = float(nutrition["energy_kcal"])
        sugar = float(nutrition["sugar_g"])
        sat_fat = float(nutrition["saturated_fat_g"])
        sodium = float(nutrition["sodium_mg"])
        fibre = float(nutrition["fibre_g"])
        protein = float(nutrition["protein_g"])
    except (KeyError, TypeError, ValueError):
        return None

    total_weight_g = float(nutrition.get("total_weight_g") or 0)
    if total_weight_g > 0:
        scale = 100.0 / total_weight_g
        basis = "per_100g_from_weight"
    else:
        scale = 1.0
        basis = "per_serving_approx"

    nutrient_values = {
        "energy": kcal * 4.184 * scale,   # kcal → kJ, then per-100g
        "sugar": sugar * scale,
        "saturated_fats": sat_fat * scale,
        "sodium": sodium * scale,
        "fibers": fibre * scale,
        "proteins": protein * scale,
        "fruit_percentage": 0.0,
    }
    breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
    breakdown["basis"] = basis
    return breakdown


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def _set_planeat_properties(recipe_id: str, rec: dict) -> None:
    """SET PLANEAT-specific properties not supported by upsert_recipe_to_neo4j."""
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (r:Recipe {recipe_id: $recipe_id})
            SET r.description               = $description,
                r.meal_type                 = $meal_type,
                r.animal_product_category   = $animal_product_category,
                r.seasonality               = $seasonality,
                r.has_planeat_nutrition     = true,
                r.ground_truth_nutrition_source = 'planeat',
                r.has_profile               = true
            """,
            {
                "recipe_id": recipe_id,
                "description": rec.get("description") or "",
                "meal_type": rec.get("meal_type") or "",
                "animal_product_category": rec.get("animal_product_category") or "",
                "seasonality": rec.get("seasonality") or [],
            },
        )


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------

def _index_elastic(recipe_id: str, rec: dict, allergens: list[str],
                   breakdown: dict | None) -> None:
    try:
        settings = get_settings()
        nutri_score = None
        nutri_color = None
        if breakdown:
            nutri_score = breakdown.get("nutri_score")
            nutri_color = breakdown.get("color")

        ingredient_names = [i if isinstance(i, str) else i["name"] for i in (rec.get("ingredients") or [])]
        doc = {
            "id": recipe_id,
            "title": rec["title"],
            "source": SOURCE,
            "source_id": SOURCE,
            "url": rec.get("url") or None,
            "image_url": rec.get("image_url") or None,
            "ingredients": ingredient_names,
            "tags": rec.get("tags") or [],
            "dish_types": rec.get("dish_types") or [],
            "allergens": allergens,
            "duration": rec.get("duration") or None,
            "serves": rec.get("serves") or 1.0,
            "expert_recipe": True,
            "has_profile": True,
            "has_planeat_nutrition": True,
            "ground_truth_nutrition_source": NUTRITION_SOURCE,
            "nutri_score_planeat": nutri_score,
            "nutri_color_planeat": nutri_color,
            "cost_category": None,
        }
        requests.put(
            f"{settings.elastic_url}/recipes_v2/_doc/{recipe_id}",
            json=doc,
            timeout=5,
        ).raise_for_status()
    except Exception as exc:
        print(f"    [ES] WARN {exc}", flush=True)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

def _upsert_postgres(recipe_id: str, rec: dict, breakdown: dict | None,
                     now_iso: str) -> None:
    nutrition = rec.get("nutrition") or {}
    serves = float(rec.get("serves") or 1)

    def _totals(suffix: str) -> dict | None:
        keys = ["energy_kcal", "protein_g", "fat_g", "saturated_fat_g",
                "carbohydrate_g", "sugar_g", "fibre_g", "sodium_mg"]
        result = {}
        for k in keys:
            v = nutrition.get(k + suffix)
            if v is not None:
                result[k] = float(v)
        return result or None

    total = _totals("")
    per_serving = _totals("_per_serving")

    nutri_score_jsonb = None
    if breakdown:
        nutri_score_jsonb = {
            "nutri_score": breakdown.get("nutri_score"),
            "color": breakdown.get("color"),
            "score": breakdown.get("score"),
        }

    details = [
        {
            "name": d.get("name"),
            "weight_g": d.get("weight_g"),
            "cofid_id": d.get("composition_food_id") or d.get("nutrition_food_id"),
            "component": d.get("component"),
            "resolution": d.get("nutrition_resolution"),
        }
        for d in (rec.get("ingredient_details") or [])
    ]

    debug = {
        "nutrition_source_file": nutrition.get("nutrition_source_file"),
        "calculation_method": nutrition.get("calculation_method"),
        "ingredient_resolution_percent": nutrition.get("ingredient_resolution_percent"),
        "quantity_coverage_percent": nutrition.get("quantity_coverage_percent"),
        "nutrient_coverage_percent": nutrition.get("nutrient_coverage_percent"),
        "serves_source": rec.get("serves_source"),
        "duration_source": rec.get("duration_source"),
        "llm_estimation": rec.get("llm_estimation"),
        "unresolved_ingredients": nutrition.get("unresolved_ingredients"),
    }

    upsert_recipe_profiling_trace({
        "recipe_id": recipe_id,
        "title": rec["title"],
        "source": SOURCE,
        "nutrition_source": NUTRITION_SOURCE,
        "total_nutrients": total,
        "total_nutrients_per_serving": per_serving,
        "nutri_score": nutri_score_jsonb,
        "nutri_score_breakdown": breakdown,
        "nutrition_profiling_details": details or None,
        "nutrition_profiling_debug": debug,
        "trace": None,
        "pipeline_version": PIPELINE_VERSION,
        "computed_at": now_iso,
    })


# ---------------------------------------------------------------------------
# Per-recipe
# ---------------------------------------------------------------------------

def process_recipe(rec: dict, write: bool) -> str:
    recipe_id = rec["recipe_id"]
    title = rec["title"]
    serves = float(rec.get("serves") or 1)
    duration = float(rec.get("duration") or 0)
    nutrition = rec.get("nutrition") or {}

    ingredient_details = rec.get("ingredient_details") or []
    computed_weight_g = sum(float(d.get("weight_g") or 0) for d in ingredient_details)
    ingredient_names = [d["name"] for d in ingredient_details]
    measurements = [d.get("measurement") or f"{d.get('weight_g', '')}g" for d in ingredient_details]
    ingredient_lines = [
        f"{d.get('measurement') or str(d.get('weight_g', ''))+chr(103)} {d['name']}"
        for d in ingredient_details
    ]
    # Instructions: list of per-dish strings from JSON
    instructions = rec.get("instructions") or []
    if isinstance(instructions, str):
        instructions = [instructions]

    allergens = detect_allergens_from_names(ingredient_names)
    nutrition_for_score = {**nutrition, "total_weight_g": computed_weight_g} if computed_weight_g > 0 else nutrition
    breakdown = _compute_nutri_score(nutrition_for_score) if nutrition else None

    if not write:
        return (
            f"DRY recipe_id={recipe_id} ingredients={len(ingredient_names)} "
            f"allergens={allergens} nutri_score={breakdown.get('nutri_score') if breakdown else None}"
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    upsert_recipe_to_neo4j(
        recipe_id=recipe_id,
        title=title,
        ingredient_lines=ingredient_lines,
        ingredient_names=ingredient_names,
        measurements=measurements,
        instructions=instructions,
        duration=duration,
        serves=serves,
        image_url=None,
        allergens=allergens,
        tags=rec.get("tags") or [],
        source=SOURCE,
        source_id=str(rec.get("source_id") or recipe_id),
        expert_recipe=True,
    )
    _set_planeat_properties(recipe_id, rec)
    _upsert_postgres(recipe_id, rec, breakdown, now_iso)
    _index_elastic(recipe_id, rec, allergens, breakdown)

    grade = breakdown.get("nutri_score") if breakdown else "n/a"
    return f"WROTE ingredients={len(ingredient_names)} allergens={allergens} grade={grade}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="Commit to Neo4j/Postgres/ES")
    ap.add_argument("--limit", type=int, default=None, help="Cap recipes for smoke test")
    ap.add_argument("--no-resume", action="store_true", help="Ignore checkpoint")
    args = ap.parse_args()

    recipes: list[dict] = json.loads(RECIPES_FILE.read_text())
    done = set() if args.no_resume else load_checkpoint()

    pending = [r for r in recipes if r["recipe_id"] not in done]
    if args.limit:
        pending = pending[: args.limit]

    print(f"PLANEAT import — {len(pending)} pending / {len(done)} already done / write={args.write}")

    imported = skipped = failed = 0
    for i, rec in enumerate(pending):
        recipe_id = rec["recipe_id"]
        print(f"[{i+1}/{len(pending)}] {recipe_id} — {rec['title'][:50]}", end=" ", flush=True)
        try:
            msg = process_recipe(rec, write=args.write)
            print(msg, flush=True)
            if args.write:
                done.add(recipe_id)
                save_checkpoint(done)
            imported += 1
        except Exception as exc:
            print(f"ERROR {exc}", flush=True)
            failed += 1

    print(f"\nDone — imported={imported} failed={failed} skipped={skipped}")


if __name__ == "__main__":
    main()
