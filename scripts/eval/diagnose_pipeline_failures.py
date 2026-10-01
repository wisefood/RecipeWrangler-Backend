#!/usr/bin/env python3
"""Step 1 of the presentation-prep plan: measure before fixing more.

1. Mass-weighted unmatched-ingredient coverage per dataset (grams, not counts --
   a 1g herb and a 1kg protein are not the same problem).
2. Outlier attribution: for every outlier recipe in data/eval/nutrition_vs_source,
   classify the cause as scaling (serves/weight, low nutrient spread but median
   ratio far from 1), matching (a heavy unmatched ingredient present), or
   reference-error (internally inconsistent ground truth), where determinable.

Read-only. Writes CSVs to data/eval/pipeline_diagnostics/.
"""
from __future__ import annotations

import csv
import os
import statistics
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
OUT_DIR = REPO_ROOT / "data/eval/pipeline_diagnostics"
OUT_DIR.mkdir(parents=True, exist_ok=True)

conn = psycopg2.connect(
    host=os.getenv("NUTRITION_HOST", "localhost"), port=os.getenv("NUTRITION_PORT", "5432"),
    dbname=os.getenv("NUTRITION_DB", "nutrients"), user=os.getenv("NUTRITION_USER", "postgres"),
    password=os.getenv("NUTRITION_PASSWORD", "postgres"),
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

SOURCES = [
    "HealthyFoods", "MyPlate", "Curated Irish Recipes", "Curated Hungarian Recipes",
    "Curated Slovenian Recipes", "FoodHero", "Best of Hungary", "The Hungary Soul",
    "Irish Heart Foundation", "SuperValu", "Slovenian Kitchen",
]
# Must match evaluate_nutrition_vs_source.py's DATASETS calc_source per source --
# measuring on 'eu' for a source the eval compares on 'irish'/'hungarian'/'slovenian'
# silently blames the wrong region's matcher for that source's real deviation.
SOURCE_REGION = {
    "Curated Irish Recipes": "irish",
    "Curated Hungarian Recipes": "hungarian",
    "Curated Slovenian Recipes": "slovenian",
    "MyPlate": "eu",
    "HealthyFoods": "eu",
}
DEFAULT_REGION = "eu"  # sources with no reference in the eval (FoodHero etc.) -- no assigned region, eu as a general default


def mass_weighted_coverage():
    """Rank unmatched ingredient names by total grams they represent, per source."""
    rows_out = []
    for source in SOURCES:
        region = SOURCE_REGION.get(source, DEFAULT_REGION)
        cur.execute(
            """SELECT elem->>'ingredient' AS ingredient, (elem->>'weight_g')::float AS w
               FROM "nutrients-recipe-profiles" p,
               LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(p.nutrition_profiling_details)='array'
                        THEN p.nutrition_profiling_details ELSE '[]'::jsonb END) elem
               WHERE p.source=%s AND p.nutrition_source=%s AND elem->>'match_confidence'='none'""",
            (source, region),
        )
        by_ing: dict[str, list[float]] = {}
        total_unmatched_g = 0.0
        for r in cur.fetchall():
            w = r["w"] or 0.0
            total_unmatched_g += w
            by_ing.setdefault(r["ingredient"], []).append(w)

        cur.execute(
            """SELECT sum((elem->>'weight_g')::float) AS total_g
               FROM "nutrients-recipe-profiles" p,
               LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(p.nutrition_profiling_details)='array'
                        THEN p.nutrition_profiling_details ELSE '[]'::jsonb END) elem
               WHERE p.source=%s AND p.nutrition_source=%s""",
            (source, region),
        )
        total_g = (cur.fetchone() or {}).get("total_g") or 1.0

        for ing, weights in sorted(by_ing.items(), key=lambda kv: -sum(kv[1]))[:30]:
            rows_out.append({
                "source": source, "ingredient": ing, "uses": len(weights),
                "total_grams": round(sum(weights), 1),
                "pct_of_source_total_mass": round(100.0 * sum(weights) / total_g, 3),
            })
        rows_out.append({"source": source, "ingredient": "__SOURCE_TOTAL_UNMATCHED_MASS_PCT__",
                          "uses": None, "total_grams": round(total_unmatched_g, 1),
                          "pct_of_source_total_mass": round(100.0 * total_unmatched_g / total_g, 2)})

    with open(OUT_DIR / "mass_weighted_unmatched.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "ingredient", "uses", "total_grams", "pct_of_source_total_mass"])
        w.writeheader()
        w.writerows(rows_out)
    print(f"mass_weighted_unmatched.csv: {len(rows_out)} rows")
    for source in SOURCES:
        source_region = SOURCE_REGION.get(source, DEFAULT_REGION)
        total_row = next(r for r in rows_out if r["source"] == source and r["ingredient"] == "__SOURCE_TOTAL_UNMATCHED_MASS_PCT__")
        print(f"  {source} ({source_region}): {total_row['pct_of_source_total_mass']}% of total ingredient mass is unmatched")


# Eval folder slug -> the same calc_source region evaluate_nutrition_vs_source.py compared it
# on. Must match DATASETS there, or this blames the wrong region's matcher for a deviation.
SLUG_REGION = {
    "curated_irish_recipes_rcsi_lab": "irish", "curated_irish_recipes_safefood_web": "irish",
    "myplate": "eu", "curated_hungarian_recipes": "hungarian",
    "curated_slovenian_recipes": "slovenian", "healthyfoods": "eu",
}


def outlier_attribution():
    """Classify every outlier recipe from the eval by likely cause."""
    eval_dir = REPO_ROOT / "data/eval/nutrition_vs_source"
    if not eval_dir.exists():
        print("No eval output found, run scripts/eval/evaluate_nutrition_vs_source.py first")
        return
    NUTRIENTS = ["energy_kcal", "protein_g", "carbohydrate_g", "fat_g",
                 "saturated_fat_g", "sugar_g", "fibre_g", "sodium_mg"]
    rows_out = []
    for sub in sorted(eval_dir.iterdir()):
        if not sub.is_dir():
            continue
        region = SLUG_REGION.get(sub.name, DEFAULT_REGION)
        outliers_csv = sub / "outliers.csv"
        details_csv = sub / "deviation_details.csv"
        if not outliers_csv.exists() or not details_csv.exists():
            continue
        outlier_ids = set()
        with open(outliers_csv) as f:
            for r in csv.DictReader(f):
                outlier_ids.add(r["recipe_id"])
        by_recipe: dict[str, dict[str, tuple[float, float]]] = {}
        titles = {}
        with open(details_csv) as f:
            for r in csv.DictReader(f):
                if r["recipe_id"] not in outlier_ids:
                    continue
                titles[r["recipe_id"]] = r["title"]
                if r["truth"] and r["calc"]:
                    t, c = float(r["truth"]), float(r["calc"])
                    if t > 0:
                        by_recipe.setdefault(r["recipe_id"], {})[r["nutrient"]] = (t, c)

        for rid, nut_pairs in by_recipe.items():
            ratios = [c / t for t, c in nut_pairs.values() if t > 0]
            if not ratios:
                continue
            median_ratio = statistics.median(ratios)
            spread = statistics.pstdev(ratios) if len(ratios) > 1 else 0.0

            # heaviest zero-contribution ingredient, if any
            cur.execute(
                """SELECT elem->>'ingredient' AS ing, (elem->>'weight_g')::float AS w
                   FROM "nutrients-recipe-profiles" p,
                   LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(p.nutrition_profiling_details)='array'
                            THEN p.nutrition_profiling_details ELSE '[]'::jsonb END) elem
                   WHERE p.recipe_id=%s AND p.nutrition_source=%s
                   AND elem->>'match_confidence'='none'
                   ORDER BY (elem->>'weight_g')::float DESC NULLS LAST LIMIT 1""",
                (rid, region),
            )
            heaviest_unmatched = cur.fetchone()

            if heaviest_unmatched and (heaviest_unmatched["w"] or 0) > 30:
                cause = "matching"
                detail = f"{heaviest_unmatched['ing']} ({heaviest_unmatched['w']:.0f}g) unmatched"
            elif spread < 0.35 and (median_ratio > 1.6 or median_ratio < 0.6):
                cause = "scaling"
                detail = f"median calc/truth ratio {median_ratio:.2f} across {len(ratios)} nutrients, low spread -- likely serves/weight, not ingredient-specific"
            elif spread >= 0.35:
                cause = "matching_or_reference"
                detail = f"high spread ({spread:.2f}) across nutrients, no single dominant unmatched ingredient found -- needs manual look"
            else:
                cause = "unclear"
                detail = f"median ratio {median_ratio:.2f}, spread {spread:.2f}"

            rows_out.append({
                "dataset": sub.name, "region": region, "recipe_id": rid, "title": titles.get(rid, ""),
                "cause": cause, "detail": detail, "median_calc_truth_ratio": round(median_ratio, 2),
                "n_nutrients_compared": len(ratios),
            })

    with open(OUT_DIR / "outlier_attribution.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["dataset", "region", "recipe_id", "title", "cause", "detail",
                                            "median_calc_truth_ratio", "n_nutrients_compared"])
        w.writeheader()
        w.writerows(rows_out)
    print(f"\noutlier_attribution.csv: {len(rows_out)} rows")
    from collections import Counter
    print(Counter(r["cause"] for r in rows_out))


if __name__ == "__main__":
    mass_weighted_coverage()
    outlier_attribution()
