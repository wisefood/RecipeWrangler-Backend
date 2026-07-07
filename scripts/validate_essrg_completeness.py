"""
Step 1.3: Validate Recipe Completeness for ESSRG Dataset

Generate CSV reports on dataset completeness:
  - overall_completeness.csv: High-level statistics
  - recipe_completeness.csv: Per-recipe field presence
  - low_coverage_recipes.txt: Recipes with missing critical fields
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

# ── config ────────────────────────────────────────────────────────────────────
RECIPES_FILE = Path("data/ESSRG/ESSRG_recipes_clean.json")
OUTPUT_DIR = Path("output/ESSRG")
COMPLETENESS_FILE = OUTPUT_DIR / "recipe_schema_completeness.csv"
SUMMARY_FILE = OUTPUT_DIR / "recipe_completeness_summary.txt"
LOW_COVERAGE_FILE = OUTPUT_DIR / "low_coverage_recipes.txt"


# ── main ──────────────────────────────────────────────────────────────────────

def validate_completeness():
    print("=" * 70)
    print("ESSRG Recipe Completeness Validation (Step 1.3)")
    print("=" * 70)

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load recipes
    print("\n[1] Loading recipes...")
    with open(RECIPES_FILE) as f:
        recipes = json.load(f)
    print(f"    Loaded {len(recipes)} recipes")

    # Define required fields
    REQUIRED_FIELDS = ["recipe_id", "source", "title", "ingredients", "serves"]
    OPTIONAL_FIELDS = ["description", "url", "image_url", "instructions", "duration_minutes", "tags", "allergens"]

    # Validate each recipe
    completeness_data = []
    issues = []

    for recipe in recipes:
        record = {
            "recipe_id": recipe.get("recipe_id"),
            "title": recipe.get("title", ""),
            "has_recipe_id": bool(recipe.get("recipe_id")),
            "has_source": bool(recipe.get("source")),
            "has_title": bool(recipe.get("title")),
            "has_ingredients": bool(recipe.get("ingredients") and len(recipe.get("ingredients", [])) > 0),
            "ingredient_count": len(recipe.get("ingredients", [])),
            "has_serves": recipe.get("serves") is not None,
            "serves": recipe.get("serves"),
            "has_description": bool(recipe.get("description")),
            "has_url": bool(recipe.get("url")),
            "has_image_url": bool(recipe.get("image_url")),
            "has_instructions": bool(recipe.get("instructions")),
            "has_duration": recipe.get("duration_minutes") is not None,
            "duration_minutes": recipe.get("duration_minutes"),
            "has_tags": bool(recipe.get("tags") and len(recipe.get("tags", [])) > 0),
            "tag_count": len(recipe.get("tags", [])),
            "has_allergens": bool(recipe.get("allergens") and len(recipe.get("allergens", [])) > 0),
            "allergen_count": len(recipe.get("allergens", [])),
            "total_weight_g": sum(ing.get("weight_g", 0) for ing in recipe.get("ingredients", [])),
        }

        completeness_data.append(record)

        # Track issues
        if not record["has_ingredients"]:
            issues.append(f"NO_INGREDIENTS: {record['title']}")
        if not record["has_description"]:
            issues.append(f"NO_DESCRIPTION: {record['title']}")
        if not record["has_instructions"]:
            issues.append(f"NO_INSTRUCTIONS: {record['title']}")
        if not record["has_serves"]:
            issues.append(f"NO_SERVES: {record['title']}")

    # Create completeness DataFrame
    df = pd.DataFrame(completeness_data)

    # Write per-recipe completeness
    print("\n[2] Writing per-recipe completeness CSV...")
    df.to_csv(COMPLETENESS_FILE, index=False)
    print(f"    Wrote {len(df)} recipes to {COMPLETENESS_FILE}")

    # Compute aggregate completeness
    print("\n[3] Computing aggregate completeness...")
    summary = {
        "Total recipes": len(recipes),
        "Recipes with recipe_id": df["has_recipe_id"].sum(),
        "Recipes with source": df["has_source"].sum(),
        "Recipes with title": df["has_title"].sum(),
        "Recipes with ingredients": df["has_ingredients"].sum(),
        "Recipes with serves": df["has_serves"].sum(),
        "Recipes with description": df["has_description"].sum(),
        "Recipes with instructions": df["has_instructions"].sum(),
        "Recipes with url": df["has_url"].sum(),
        "Recipes with image_url": df["has_image_url"].sum(),
        "Recipes with duration": df["has_duration"].sum(),
        "Recipes with tags": df["has_tags"].sum(),
        "Recipes with allergens": df["has_allergens"].sum(),
    }

    summary_pct = {
        k: f"{(v / len(recipes) * 100):.1f}%" for k, v in summary.items()
    }

    # Write summary
    print(f"\n[4] Writing summary to {SUMMARY_FILE}...")
    with open(SUMMARY_FILE, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("ESSRG Recipe Completeness Summary (Step 1.3)\n")
        f.write("=" * 70 + "\n\n")

        f.write("SCHEMA COMPLETENESS\n")
        f.write("-" * 70 + "\n")
        for key in summary:
            count = summary[key]
            pct = summary_pct[key]
            f.write(f"{key:40} {count:4} ({pct})\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("INGREDIENT STATISTICS\n")
        f.write("-" * 70 + "\n")
        f.write(f"Recipes with ≥1 ingredient:      {df['has_ingredients'].sum()}\n")
        f.write(f"Average ingredients per recipe:  {df['ingredient_count'].mean():.1f}\n")
        f.write(f"Min ingredients:                 {df['ingredient_count'].min()}\n")
        f.write(f"Max ingredients:                 {df['ingredient_count'].max()}\n")
        f.write(f"Average total weight/recipe:     {df['total_weight_g'].mean():.0f}g\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("TAG STATISTICS\n")
        f.write("-" * 70 + "\n")
        f.write(f"Recipes with ≥1 tag:             {df['has_tags'].sum()}\n")
        f.write(f"Average tags per recipe:         {df['tag_count'].mean():.1f}\n")

        f.write("\n" + "-" * 70 + "\n")
        f.write("CRITICAL ISSUES\n")
        f.write("-" * 70 + "\n")
        f.write(f"Recipes with no ingredients:     {len([i for i in issues if 'NO_INGREDIENTS' in i])}\n")
        f.write(f"Recipes with no serves:          {len([i for i in issues if 'NO_SERVES' in i])}\n")
        f.write(f"Recipes with no instructions:    {len([i for i in issues if 'NO_INSTRUCTIONS' in i])}\n")
        f.write(f"Recipes with no description:     {len([i for i in issues if 'NO_DESCRIPTION' in i])}\n")

    print(f"    Wrote summary to {SUMMARY_FILE}")

    # Write low-coverage recipes (missing multiple critical fields)
    print(f"\n[5] Identifying low-coverage recipes...")
    low_coverage = []
    for _, row in df.iterrows():
        missing_count = 0
        missing_fields = []

        for field in REQUIRED_FIELDS:
            has_field_col = f"has_{field}"
            if has_field_col in row and not row[has_field_col]:
                missing_count += 1
                missing_fields.append(field)

        # Flag as low-coverage if missing critical fields or has 0 ingredients
        if row["ingredient_count"] == 0 or missing_count >= 2:
            low_coverage.append(
                {
                    "recipe_id": row["recipe_id"],
                    "title": row["title"],
                    "missing_fields": ",".join(missing_fields),
                    "ingredient_count": row["ingredient_count"],
                    "issue_severity": "CRITICAL" if row["ingredient_count"] == 0 else "WARNING",
                }
            )

    with open(LOW_COVERAGE_FILE, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("Low-Coverage Recipes\n")
        f.write("=" * 70 + "\n\n")

        if low_coverage:
            f.write(f"Total low-coverage recipes: {len(low_coverage)}\n\n")
            for item in low_coverage:
                f.write(f"[{item['issue_severity']}] {item['recipe_id']}\n")
                f.write(f"  Title: {item['title']}\n")
                f.write(f"  Missing: {item['missing_fields']}\n")
                f.write(f"  Ingredients: {item['ingredient_count']}\n\n")
        else:
            f.write("No low-coverage recipes found.\n")

    print(f"    Found {len(low_coverage)} low-coverage recipes")
    print(f"    Wrote to {LOW_COVERAGE_FILE}")

    # Print summary to console
    print("\n" + "=" * 70)
    print("COMPLETENESS SUMMARY")
    print("=" * 70)
    for key in [
        "Total recipes",
        "Recipes with recipe_id",
        "Recipes with source",
        "Recipes with title",
        "Recipes with ingredients",
        "Recipes with serves",
    ]:
        count = summary[key]
        pct = summary_pct[key]
        print(f"{key:40} {count:4} ({pct})")

    print(f"\nIngredient Statistics:")
    print(f"  Average per recipe: {df['ingredient_count'].mean():.1f}")
    print(f"  Average weight:     {df['total_weight_g'].mean():.0f}g")

    print(f"\nCritical Issues:")
    print(f"  Recipes with 0 ingredients: {(df['ingredient_count'] == 0).sum()}")
    print(f"  Recipes missing serves:     {(~df['has_serves']).sum()}")

    print("\n[✓] Validation complete")


if __name__ == "__main__":
    validate_completeness()
