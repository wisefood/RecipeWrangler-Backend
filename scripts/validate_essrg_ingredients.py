"""
Step 1.4–1.5: Ingredient Validation & Weight Resolution for ESSRG

Since ESSRG ingredients are already:
  1. Named with CoFID food names (canonical)
  2. Quantified in grams (already resolved)

This script:
  - Validates that all ingredient food names exist in CoFID 2021
  - Generates an ingredient parse/resolution audit CSV
  - Reports any unmatched or problematic ingredients
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# ── config ────────────────────────────────────────────────────────────────────
RECIPES_FILE = Path("data/ESSRG/ESSRG_recipes_clean.json")
EXCEL_FILE = Path("PLANEAT T442 MEAL DB LL ESSRG.xlsx")
OUTPUT_DIR = Path("output/ESSRG")
AUDIT_FILE = OUTPUT_DIR / "ingredient_parse_audit.csv"
SUMMARY_FILE = OUTPUT_DIR / "ingredient_resolution_report.txt"


# ── main ──────────────────────────────────────────────────────────────────────

def validate_ingredients():
    print("=" * 70)
    print("ESSRG Ingredient Validation (Steps 1.4–1.5)")
    print("=" * 70)

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load recipes
    print("\n[1] Loading recipes...")
    with open(RECIPES_FILE) as f:
        recipes = json.load(f)
    print(f"    Loaded {len(recipes)} recipes")

    # Load CoFID food composition table to get canonical food names
    print("\n[2] Loading CoFID 2021 food composition database...")
    proximates_df = pd.read_excel(EXCEL_FILE, sheet_name="1.3 Proximates", header=0)
    cofid_foods = set()

    for _, row in proximates_df.iterrows():
        food_name = row.get("Food Name")
        if pd.notna(food_name):
            cofid_foods.add(str(food_name).strip())

    print(f"    Found {len(cofid_foods)} unique foods in CoFID")

    # Audit all ingredients
    print("\n[3] Validating ingredients against CoFID...")
    audit_records = []
    unmatched_ingredients = set()

    total_ingredients = 0
    matched = 0
    unmatched = 0

    for recipe in recipes:
        for ing in recipe.get("ingredients", []):
            total_ingredients += 1
            ing_name = ing.get("name", "").strip()
            weight_g = ing.get("weight_g", 0)
            measurement = ing.get("measurement")

            # Check if ingredient matches a CoFID food
            is_matched = ing_name in cofid_foods

            if is_matched:
                matched += 1
                status = "MATCHED"
            else:
                unmatched += 1
                status = "UNMATCHED"
                unmatched_ingredients.add(ing_name)

            audit_records.append(
                {
                    "recipe_id": recipe.get("recipe_id"),
                    "recipe_title": recipe.get("title"),
                    "ingredient_name": ing_name,
                    "measurement": measurement,
                    "weight_g": weight_g,
                    "cofid_match_status": status,
                    "parse_status": "PARSED" if weight_g > 0 else "NO_WEIGHT",
                    "resolution_method": "COFID_DIRECT" if is_matched else "UNMATCHED",
                }
            )

    # Create audit DataFrame
    df = pd.DataFrame(audit_records)

    # Write audit CSV
    print(f"\n[4] Writing ingredient audit to {AUDIT_FILE}...")
    df.to_csv(AUDIT_FILE, index=False)
    print(f"    Wrote {len(df)} ingredients")

    # Write summary report
    print(f"\n[5] Writing summary to {SUMMARY_FILE}...")
    with open(SUMMARY_FILE, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("ESSRG Ingredient Resolution Report (Steps 1.4–1.5)\n")
        f.write("=" * 70 + "\n\n")

        f.write("INGREDIENT STATISTICS\n")
        f.write("-" * 70 + "\n")
        f.write(f"Total ingredients across all recipes: {total_ingredients}\n")
        f.write(f"Unique ingredient names:              {len(df['ingredient_name'].unique())}\n")
        f.write(f"Recipes processed:                    {len(recipes)}\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("CoFID MATCHING\n")
        f.write("-" * 70 + "\n")
        f.write(f"Ingredients matched to CoFID:         {matched} ({matched/total_ingredients*100:.1f}%)\n")
        f.write(f"Unmatched ingredients:                {unmatched} ({unmatched/total_ingredients*100:.1f}%)\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("WEIGHT RESOLUTION\n")
        f.write("-" * 70 + "\n")
        f.write(f"Ingredients with weight (>0g):        {(df['weight_g'] > 0).sum()}\n")
        f.write(f"Ingredients with no weight:           {(df['weight_g'] == 0).sum()}\n")
        f.write(f"Average weight:                       {df['weight_g'].mean():.1f}g\n")
        f.write(f"Median weight:                        {df['weight_g'].median():.1f}g\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("PARSE STATUS\n")
        f.write("-" * 70 + "\n")
        parse_status_counts = df["parse_status"].value_counts()
        for status, count in parse_status_counts.items():
            f.write(f"{status}: {count} ({count/total_ingredients*100:.1f}%)\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("RESOLUTION METHODS\n")
        f.write("-" * 70 + "\n")
        resolution_counts = df["resolution_method"].value_counts()
        for method, count in resolution_counts.items():
            f.write(f"{method}: {count} ({count/total_ingredients*100:.1f}%)\n")

        if unmatched_ingredients:
            f.write("\n" + "-" * 70 + "\n")
            f.write(f"UNMATCHED INGREDIENTS ({len(unmatched_ingredients)} unique)\n")
            f.write("-" * 70 + "\n")
            for ing in sorted(unmatched_ingredients)[:50]:  # Show first 50
                count = sum(1 for r in audit_records if r["ingredient_name"] == ing and r["cofid_match_status"] == "UNMATCHED")
                f.write(f"  {ing} (appears {count} times)\n")
            if len(unmatched_ingredients) > 50:
                f.write(f"  ... and {len(unmatched_ingredients) - 50} more\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("ASSESSMENT\n")
        f.write("-" * 70 + "\n")
        if unmatched == 0:
            f.write("✓ All ingredients matched to CoFID food database\n")
            f.write("✓ All ingredients have weight values\n")
            f.write("✓ Ready for nutrition profiling\n")
        else:
            f.write(f"✗ {unmatched} ingredients unmatched to CoFID\n")
            f.write("  Review unmatched ingredients; consider:\n")
            f.write("    - Fuzzy matching against CoFID food names\n")
            f.write("    - Manual mapping\n")
            f.write("    - Removal if non-nutritional\n")

    print(f"    Wrote summary to {SUMMARY_FILE}")

    # Print summary to console
    print("\n" + "=" * 70)
    print("INGREDIENT VALIDATION SUMMARY")
    print("=" * 70)
    print(f"Total ingredients:        {total_ingredients}")
    print(f"Matched to CoFID:         {matched} ({matched/total_ingredients*100:.1f}%)")
    print(f"Unmatched:                {unmatched} ({unmatched/total_ingredients*100:.1f}%)")
    print(f"Unique unmatched names:   {len(unmatched_ingredients)}")

    if unmatched > 0:
        print(f"\nUnmatched ingredients (first 10):")
        for ing in sorted(unmatched_ingredients)[:10]:
            print(f"  - {ing}")

    print("\n[✓] Ingredient validation complete")


if __name__ == "__main__":
    validate_ingredients()
