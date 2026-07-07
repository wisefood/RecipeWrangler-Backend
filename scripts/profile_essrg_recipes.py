"""
Step 1.6: Profile ESSRG Recipes Against CoFID 2021 Composition Table

For each recipe:
  1. Look up each ingredient food name in CoFID composition tables
  2. Aggregate nutrients (per-100g × quantity → totals)
  3. Calculate per-serving values (totals ÷ serves)
  4. Compute Nutri-Score grade (A–E)
  5. Track ingredient match coverage

Output:
  - nutrition_profiles.csv: Recipe-level nutrition totals
  - nutrition_coverage.csv: Ingredient match stats per recipe
  - low_coverage_recipes.csv: Recipes with <80% ingredient coverage
  - recipes_with_profiles.json: Full profile data in JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Import Nutri-Score calculation if available
try:
    from recipe_wrangler.utils.nutri_score import calculate_nutri_score
except ImportError:
    calculate_nutri_score = None

# ── config ────────────────────────────────────────────────────────────────────
RECIPES_FILE = Path("data/ESSRG/ESSRG_recipes_clean.json")
EXCEL_FILE = Path("PLANEAT T442 MEAL DB LL ESSRG.xlsx")
OUTPUT_DIR = Path("output/ESSRG")
PROFILES_CSV = OUTPUT_DIR / "nutrition_profiles.csv"
COVERAGE_CSV = OUTPUT_DIR / "nutrition_coverage.csv"
LOW_COVERAGE_CSV = OUTPUT_DIR / "low_coverage_recipes.csv"
PROFILES_JSON = OUTPUT_DIR / "recipes_with_profiles.json"

# Core nutrients for Nutri-Score (required fields)
NUTRI_SCORE_FIELDS = [
    "energy_kcal",
    "protein_g",
    "fat_g",
    "saturated_fat_g",
    "carbohydrate_g",
    "sugars_g",
    "fiber_g",
    "sodium_mg",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def build_cofid_lookup(excel_file: Path) -> dict[str, dict[str, float]]:
    """
    Build a lookup: food_name → {nutrient_name: value_per_100g, ...}

    Combines data from multiple CoFID sheets:
    - 1.3 Proximates (main macronutrients, energy, fiber, sugars)
    - 1.4 Inorganics (sodium, etc.)
    - 1.5 Vitamins (optional)
    """
    print("[*] Loading CoFID composition tables...")

    # Load sheets
    proximates = pd.read_excel(excel_file, sheet_name="1.3 Proximates", header=0)
    inorganics = pd.read_excel(excel_file, sheet_name="1.4 Inorganics", header=0)

    lookup = {}

    # Process each food in proximates
    for idx, row in proximates.iterrows():
        food_name = row.get("Food Name")
        if not food_name or pd.isna(food_name):
            continue

        food_name = str(food_name).strip()

        # Extract macronutrients from proximates
        nutrients = {
            "energy_kcal": _safe_float(row.get("Energy (kcal) (kcal)")),
            "protein_g": _safe_float(row.get("Protein (g)")),
            "fat_g": _safe_float(row.get("Fat (g)")),
            "saturated_fat_g": _safe_float(row.get("Satd FA /100g fd (g)")),
            "carbohydrate_g": _safe_float(row.get("Carbohydrate (g)")),
            "sugars_g": _safe_float(row.get("Total sugars (g)")),
            "fiber_g": _safe_float(
                row.get("NSP (g)") or row.get("AOAC fibre (g)")
            ),
            "water_g": _safe_float(row.get("Water (g)")),
            "cholesterol_mg": _safe_float(row.get("Cholesterol (mg)")),
        }

        lookup[food_name] = nutrients

    # Add sodium from inorganics
    for idx, row in inorganics.iterrows():
        food_name = row.get("Food Name")
        if not food_name or pd.isna(food_name):
            continue

        food_name = str(food_name).strip()

        if food_name in lookup:
            sodium = _safe_float(row.get("Sodium (mg)"))
            if sodium is not None:
                lookup[food_name]["sodium_mg"] = sodium

    print(f"    Built CoFID lookup with {len(lookup)} foods")
    return lookup


def _safe_float(value: Any) -> Optional[float]:
    """Convert value to float, handling NaN, 'N', 'Tr', etc."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        v = str(value).strip().upper()
        if v in ["N", "NAN", "", "TR", "TRACE"]:
            return None
        try:
            return float(v)
        except ValueError:
            return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def profile_recipe(
    recipe: dict[str, Any],
    cofid_lookup: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """
    Profile a single recipe by aggregating ingredient nutrients from CoFID.

    Returns:
      {
        recipe_id: str,
        title: str,
        total_weight_g: float,
        total_matched_weight_g: float,
        coverage_percent: float,
        [nutrient names]: float,
        [nutrient names]_per_serving: float,
        nutri_score_grade: str,
        profiling_notes: [str],
      }
    """
    profile = {
        "recipe_id": recipe.get("recipe_id"),
        "title": recipe.get("title"),
        "serves": recipe.get("serves", 1.0),
        "profiling_notes": [],
    }

    ingredients = recipe.get("ingredients", [])
    total_weight_g = 0.0
    matched_weight_g = 0.0
    unmatched_weight_g = 0.0

    # Initialize nutrient accumulators
    nutrients = defaultdict(float)

    # Process each ingredient
    for ing in ingredients:
        ing_name = ing.get("name", "").strip()
        weight_g = ing.get("weight_g", 0.0)
        total_weight_g += weight_g

        # Look up food in CoFID
        if ing_name not in cofid_lookup:
            unmatched_weight_g += weight_g
            profile["profiling_notes"].append(f"Unmatched ingredient: {ing_name}")
            continue

        matched_weight_g += weight_g
        cofid_food = cofid_lookup[ing_name]

        # Aggregate nutrients
        for nutrient, value_per_100g in cofid_food.items():
            if value_per_100g is not None:
                # Scale from per-100g to actual weight
                value_total = value_per_100g * (weight_g / 100.0)
                nutrients[nutrient] += value_total

    # Calculate coverage
    coverage_percent = (matched_weight_g / total_weight_g * 100.0) if total_weight_g > 0 else 0.0

    profile["total_weight_g"] = total_weight_g
    profile["total_matched_weight_g"] = matched_weight_g
    profile["total_unmatched_weight_g"] = unmatched_weight_g
    profile["coverage_percent"] = coverage_percent

    # Set nutrient totals
    for nutrient in NUTRI_SCORE_FIELDS + ["water_g", "cholesterol_mg"]:
        value = nutrients.get(nutrient, 0.0)
        profile[nutrient] = value

        # Per-serving
        serves = profile["serves"]
        profile[f"{nutrient}_per_serving"] = value / serves if serves > 0 else 0.0

    # Calculate Nutri-Score if available
    if calculate_nutri_score:
        try:
            nutri_score = calculate_nutri_score(
                energy_kcal=profile.get("energy_kcal", 0),
                protein_g=profile.get("protein_g", 0),
                fat_g=profile.get("fat_g", 0),
                saturated_fat_g=profile.get("saturated_fat_g", 0),
                carbohydrate_g=profile.get("carbohydrate_g", 0),
                sugars_g=profile.get("sugars_g", 0),
                fiber_g=profile.get("fiber_g", 0),
                sodium_mg=profile.get("sodium_mg", 0),
            )
            if nutri_score:
                profile["nutri_score_grade"] = nutri_score.get("grade", "?")
                profile["nutri_score_score"] = nutri_score.get("score", None)
        except Exception as e:
            profile["profiling_notes"].append(f"Nutri-Score calculation failed: {str(e)}")
    else:
        profile["nutri_score_grade"] = None
        profile["nutri_score_score"] = None

    return profile


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("ESSRG Recipe Profiling Against CoFID 2021 (Step 1.6)")
    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load recipes
    print("\n[1] Loading recipes...")
    with open(RECIPES_FILE) as f:
        recipes = json.load(f)
    print(f"    Loaded {len(recipes)} recipes")

    # Build CoFID lookup
    print("\n[2] Building CoFID composition lookup...")
    cofid_lookup = build_cofid_lookup(EXCEL_FILE)

    # Profile each recipe
    print("\n[3] Profiling recipes...")
    profiles = []
    coverage_data = []
    low_coverage = []

    for i, recipe in enumerate(recipes, 1):
        profile = profile_recipe(recipe, cofid_lookup)
        profiles.append(profile)

        # Track coverage
        coverage_data.append({
            "recipe_id": profile["recipe_id"],
            "title": profile["title"],
            "total_ingredients": len(recipe.get("ingredients", [])),
            "total_weight_g": profile["total_weight_g"],
            "matched_weight_g": profile["total_matched_weight_g"],
            "unmatched_weight_g": profile["total_unmatched_weight_g"],
            "coverage_percent": profile["coverage_percent"],
        })

        # Flag low coverage
        if profile["coverage_percent"] < 80.0:
            low_coverage.append(profile)

        if i % 50 == 0:
            print(f"    Profiled {i}/{len(recipes)} recipes...")

    print(f"    Profiled {len(profiles)} recipes")

    # Write profiles CSV
    print(f"\n[4] Writing nutrition profiles to {PROFILES_CSV}...")
    profiles_df = pd.DataFrame([
        {
            "recipe_id": p["recipe_id"],
            "title": p["title"],
            "serves": p["serves"],
            "total_weight_g": p["total_weight_g"],
            "energy_kcal": p.get("energy_kcal", 0),
            "protein_g": p.get("protein_g", 0),
            "fat_g": p.get("fat_g", 0),
            "saturated_fat_g": p.get("saturated_fat_g", 0),
            "carbohydrate_g": p.get("carbohydrate_g", 0),
            "sugars_g": p.get("sugars_g", 0),
            "fiber_g": p.get("fiber_g", 0),
            "sodium_mg": p.get("sodium_mg", 0),
            "energy_kcal_per_serving": p.get("energy_kcal_per_serving", 0),
            "protein_g_per_serving": p.get("protein_g_per_serving", 0),
            "fat_g_per_serving": p.get("fat_g_per_serving", 0),
            "carbohydrate_g_per_serving": p.get("carbohydrate_g_per_serving", 0),
            "nutri_score_grade": p.get("nutri_score_grade", "?"),
        }
        for p in profiles
    ])
    profiles_df.to_csv(PROFILES_CSV, index=False)
    print(f"    Wrote {len(profiles_df)} profiles")

    # Write coverage CSV
    print(f"\n[5] Writing coverage audit to {COVERAGE_CSV}...")
    coverage_df = pd.DataFrame(coverage_data)
    coverage_df.to_csv(COVERAGE_CSV, index=False)
    print(f"    Wrote coverage data for {len(coverage_df)} recipes")

    # Write low-coverage CSV
    if low_coverage:
        print(f"\n[6] Writing low-coverage recipes to {LOW_COVERAGE_CSV}...")
        low_cov_df = pd.DataFrame([
            {
                "recipe_id": p["recipe_id"],
                "title": p["title"],
                "coverage_percent": p["coverage_percent"],
                "matched_weight_g": p["total_matched_weight_g"],
                "total_weight_g": p["total_weight_g"],
            }
            for p in low_coverage
        ])
        low_cov_df.to_csv(LOW_COVERAGE_CSV, index=False)
        print(f"    Wrote {len(low_cov_df)} low-coverage recipes")

    # Write profiles JSON
    print(f"\n[7] Writing full profiles to {PROFILES_JSON}...")
    with open(PROFILES_JSON, "w") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
    print(f"    Wrote {len(profiles)} full profiles")

    # Summary statistics
    print("\n" + "=" * 70)
    print("PROFILING SUMMARY")
    print("=" * 70)

    coverage_arr = np.array([p["coverage_percent"] for p in profiles])
    print(f"Recipes profiled:               {len(profiles)}")
    print(f"Average coverage:               {coverage_arr.mean():.1f}%")
    print(f"Min coverage:                   {coverage_arr.min():.1f}%")
    print(f"Max coverage:                   {coverage_arr.max():.1f}%")
    print(f"Recipes with <80% coverage:     {len(low_coverage)}")

    # Nutrient stats
    energy_vals = np.array([p.get("energy_kcal", 0) for p in profiles])
    print(f"\nNutrition Statistics (per recipe):")
    print(f"  Energy:           {energy_vals.mean():.0f} ± {energy_vals.std():.0f} kcal")
    print(f"  Protein:          {np.array([p.get('protein_g', 0) for p in profiles]).mean():.1f}g")
    print(f"  Fat:              {np.array([p.get('fat_g', 0) for p in profiles]).mean():.1f}g")
    print(f"  Carbohydrate:     {np.array([p.get('carbohydrate_g', 0) for p in profiles]).mean():.1f}g")

    # Nutri-Score distribution
    if profiles[0].get("nutri_score_grade"):
        grades = [p.get("nutri_score_grade", "?") for p in profiles]
        grade_counts = {}
        for g in grades:
            grade_counts[g] = grade_counts.get(g, 0) + 1
        print(f"\nNutri-Score Distribution:")
        for grade in sorted(grade_counts.keys()):
            count = grade_counts[grade]
            print(f"  Grade {grade}: {count} recipes ({count/len(profiles)*100:.1f}%)")

    print("\n[✓] Profiling complete")


if __name__ == "__main__":
    main()
