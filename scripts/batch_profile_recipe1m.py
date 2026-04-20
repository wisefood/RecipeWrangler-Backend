"""
Batch-profile recipe1m recipes that already have pre-computed weights.

Skips the weight estimation step entirely — reads weight_per_ingr from
recipes_with_nutritional_info.json and feeds them directly into
Recipe_Profiling_Node for all three regions (US/IE/HU).

Output: data_to_send/nutrition_comparison_full_<date>.csv
Checkpoint: data_to_send/nutrition_comparison_full_<date>.checkpoint.json
             (resume-safe — already-done recipe IDs stored here)
"""

import json
import os
import sys
import csv
import time
import signal
from datetime import date
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recipe_wrangler.schemas.models import RecipeState
from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Node

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INPUT_FILE = Path("data/processed/recipe1m/recipes_with_nutritional_info.json")
OUT_DIR = Path("data_to_send")
STAMP = date.today().isoformat()
OUT_CSV = OUT_DIR / f"nutrition_comparison_full_{STAMP}.csv"
CHECKPOINT_FILE = OUT_DIR / f"nutrition_comparison_full_{STAMP}.checkpoint.json"
REGIONS = ["US", "IE", "HU"]
REGION_SOURCE = {"US": "usda", "IE": "irish", "HU": "hungarian"}

CSV_FIELDS = [
    "recipe_id", "title", "source",
    "energy_kcal", "protein_g", "fat_g", "sugars_g",
    "nutri_score_value", "nutri_score_label",
]

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_stop = False

def _handle_signal(sig, frame):
    global _stop
    print("\n[batch] Signal received — will stop after current recipe.", flush=True)
    _stop = True

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done: set):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(done), f)


def original_row(r: dict) -> dict:
    """Extract per-100g values scaled to total recipe from recipe1m source."""
    nutr = r.get("nutr_values_per100g", {})
    total_weight = sum(r.get("weight_per_ingr", []) or [])
    scale = total_weight / 100.0 if total_weight else 0.0
    return {
        "recipe_id": r["id"],
        "title": r["title"],
        "source": "recipe1m_original",
        "energy_kcal": round(nutr.get("energy", 0) * scale, 4),
        "protein_g": round(nutr.get("protein", 0) * scale, 4),
        "fat_g": round(nutr.get("fat", 0) * scale, 4),
        "sugars_g": round(nutr.get("sugars", 0) * scale, 4),
        "nutri_score_value": "",
        "nutri_score_label": "",
    }


def pipeline_row(recipe_id: str, title: str, region: str, state_result: RecipeState) -> dict:
    totals = state_result.profiling_totals or {}
    ns = state_result.nutri_score or {}
    src = REGION_SOURCE[region]

    def g(key):
        return round(totals.get(f"total_{key}_{src}", 0) or 0, 4)

    return {
        "recipe_id": recipe_id,
        "title": title,
        "source": f"pipeline_{src}",
        "energy_kcal": g("energy_kcal"),
        "protein_g": g("protein_g"),
        "fat_g": g("fat_g"),
        "sugars_g": g("sugar_g"),
        "nutri_score_value": ns.get("score", ""),
        "nutri_score_label": ns.get("label", ""),
    }


def profile_recipe(r: dict, region: str) -> RecipeState:
    names = [i["text"] for i in r["ingredients"]]
    measurements = [
        f"{q['text']} {u['text']}"
        for q, u in zip(r["quantity"], r["unit"])
    ]
    weights = [float(w) if w else 0.0 for w in r.get("weight_per_ingr", [])]

    state = RecipeState(
        title=r["title"],
        ingredient_names=names,
        measurements=measurements,
        weights=weights,
        serves=float(r.get("serves") or 1),
        region=region,
    )
    return Recipe_Profiling_Node(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"[batch] Loading {INPUT_FILE} ...", flush=True)
    with open(INPUT_FILE) as f:
        recipes = json.load(f)
    print(f"[batch] {len(recipes)} recipes loaded.", flush=True)

    done = load_checkpoint()
    print(f"[batch] Checkpoint: {len(done)} recipes already done.", flush=True)

    file_exists = OUT_CSV.exists()
    csv_file = open(OUT_CSV, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if not file_exists:
        writer.writeheader()

    todo = [r for r in recipes if r["id"] not in done]
    total = len(todo)
    print(f"[batch] {total} recipes remaining. Output: {OUT_CSV}", flush=True)

    t_start = time.time()
    errors = 0

    for i, r in enumerate(todo):
        if _stop:
            break

        recipe_id = r["id"]
        try:
            writer.writerow(original_row(r))

            for region in REGIONS:
                result = profile_recipe(r, region)
                writer.writerow(pipeline_row(recipe_id, r["title"], region, result))

            csv_file.flush()
            done.add(recipe_id)

        except Exception as e:
            errors += 1
            print(f"[batch] ERROR {recipe_id}: {e}", flush=True)
            continue

        if (i + 1) % 100 == 0:
            save_checkpoint(done)
            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed
            remaining = (total - i - 1) / rate / 3600 if rate > 0 else 0
            print(
                f"[batch] {i+1}/{total} done | {rate:.2f} rec/s | "
                f"~{remaining:.1f}h remaining | errors: {errors}",
                flush=True,
            )

    save_checkpoint(done)
    csv_file.close()

    elapsed = time.time() - t_start
    print(
        f"[batch] Finished. {len(done)} recipes profiled in {elapsed/3600:.2f}h. "
        f"Errors: {errors}. Output: {OUT_CSV}",
        flush=True,
    )


if __name__ == "__main__":
    main()
