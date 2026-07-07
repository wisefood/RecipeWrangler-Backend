#!/usr/bin/env python3
"""Calculate deterministic FVLN percentages for the Section 5 reference subsets."""

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd


REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "section5_outputs"
MAPPINGS = REPO / "data/mappings/recipe1m-usda-links-canonical.json"
USDA_NUTRIENTS = REPO / "data/processed/usda/usda-nutrients-v1.json"
HEALTHY_REFERENCE = REPO / "data/HealthyFoods/HealthyFood_recipes_nutrition_clean.json"
DETAIL_OUTPUT = OUT / "reference_recipe_fvln_percentages.csv"
STATS_OUTPUT = OUT / "descriptive_nutriscore_input_stats.csv"
PLOT_STATS_OUTPUT = OUT / "reference_nutriscore_input_medians_for_plot.csv"

ELIGIBLE_PREFIXES = {"09", "11", "12", "16"}
ELIGIBLE_OILS = {"olive oil", "olive oils", "rapeseed oil", "canola oil", "walnut oil"}
SOURCE_LABELS = {
    "Curated Irish Recipes": "RCSI SafeFood",
    "HealthyFoods": "HealthyFoods",
    "recipe1m": "Recipe1M / HUMMUS profiled overlap",
}


def normalize(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^\w\s-]", " ", text)
    return " ".join(text.split())


def psql_csv(query: str) -> pd.DataFrame:
    sql = f"COPY ({query.rstrip().rstrip(';')}) TO STDOUT WITH CSV HEADER;\n"
    result = subprocess.run(
        ["docker", "exec", "-i", "wisefood-postgres", "psql", "-U", "postgres",
         "-d", "nutrients", "-v", "ON_ERROR_STOP=1"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return pd.read_csv(io.StringIO(result.stdout), dtype=str)


def load_name_maps() -> tuple[dict[str, str], dict[str, str]]:
    rows = json.loads(MAPPINGS.read_text(encoding="utf-8"))
    canonical: dict[str, str] = {}
    labels: dict[str, str] = {}
    for row in rows:
        usda_id = str(row.get("usda_id") or "").strip()
        if not usda_id:
            continue
        canonical.setdefault(normalize(row.get("canonical")), usda_id)
        labels.setdefault(normalize(row.get("usda_food_label")), usda_id)
    for row in json.loads(USDA_NUTRIENTS.read_text(encoding="utf-8")):
        usda_id = str(row.get("usda_id") or "").strip()
        food_name = normalize(row.get("food_name"))
        if usda_id and food_name:
            labels.setdefault(food_name, usda_id)
    return canonical, labels


def healthy_reference_titles() -> set[str]:
    payload = json.loads(HEALTHY_REFERENCE.read_text(encoding="utf-8"))
    titles = set()
    for recipe in payload.get("recipes", []):
        nutrition = recipe.get("nutrition_per_serve") or {}
        energy_available = nutrition.get("Calories") is not None or nutrition.get("Kilojoules") is not None
        if energy_available:
            titles.add(normalize(recipe.get("title")))
    return titles


def load_profiles() -> pd.DataFrame:
    return psql_csv(
        """
        SELECT source, recipe_id, title, details
        FROM (
          SELECT p.source, p.recipe_id, p.title,
                 p.nutrition_profiling_details::text AS details,
                 ROW_NUMBER() OVER (
                   PARTITION BY p.source, p.recipe_id
                   ORDER BY CASE p.nutrition_source WHEN 'usda' THEN 0 ELSE 1 END
                 ) AS preference
          FROM public."nutrients-recipe-profiles" p
          WHERE p.nutrition_source IN ('usda', 'eu')
          AND (
            (p.source = 'Curated Irish Recipes' AND EXISTS (
              SELECT 1 FROM public."nutrients-recipe-profiles" r
              WHERE r.source=p.source AND r.recipe_id=p.recipe_id
                AND r.nutrition_source='safefood_rcsi'
            ))
            OR
            (p.source = 'HealthyFoods')
            OR
            (p.source = 'recipe1m' AND EXISTS (
              SELECT 1 FROM public."nutrients-recipe-profiles" r
              WHERE r.source=p.source AND r.recipe_id=p.recipe_id
                AND r.nutrition_source='recipe1m_original'
                AND r.nutri_score IS NOT NULL
            ))
          )
        ) candidates
        WHERE preference = 1
        """
    )


def resolve_usda_id(item: dict[str, Any], canonical: dict[str, str], labels: dict[str, str]) -> tuple[str | None, str]:
    identifier = str(item.get("canonical_food_id") or "").strip()
    if len(identifier) >= 2 and identifier[:2].isdigit():
        return identifier, "numeric_canonical_id"
    for field, mapping, method in (
        ("name", canonical, "cached_canonical_name"),
        ("name", labels, "cached_usda_label"),
        ("matched_nutritional_ingredient", labels, "cached_matched_label"),
        ("matched_nutritional_ingredient", canonical, "cached_matched_canonical"),
    ):
        key = normalize(item.get(field))
        if key and key in mapping:
            return mapping[key], method
    return None, "unresolved"


def calculate_row(record: dict[str, Any], canonical: dict[str, str], labels: dict[str, str]) -> dict[str, Any]:
    details = json.loads(record.get("details") or "[]")
    total_weight = 0.0
    classified_weight = 0.0
    eligible_weight = 0.0
    weighted_items = 0
    classified_items = 0
    for item in details:
        try:
            weight = float(item.get("weight_g") or 0.0)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        weighted_items += 1
        total_weight += weight
        name = normalize(item.get("name") or item.get("ingredient"))
        usda_id, _ = resolve_usda_id(item, canonical, labels)
        if usda_id:
            classified_items += 1
            classified_weight += weight
        if name in ELIGIBLE_OILS or (usda_id and usda_id[:2] in ELIGIBLE_PREFIXES):
            eligible_weight += weight
    percentage = eligible_weight / total_weight * 100.0 if total_weight else float("nan")
    coverage = classified_weight / total_weight * 100.0 if total_weight else 0.0
    return {
        "reference_source_or_subset": SOURCE_LABELS[record["source"]],
        "recipe_id": record["recipe_id"],
        "title": record["title"],
        "fvln_percent": percentage,
        "eligible_weight_g": eligible_weight,
        "total_recipe_weight_g": total_weight,
        "classified_weight_coverage_pct": coverage,
        "weighted_ingredient_count": weighted_items,
        "classified_ingredient_count": classified_items,
        "calculation_status": "complete" if coverage >= 99.999 else "partial_mapping",
        "provenance": "RecipeWrangler estimate from cached EU-profile weights and cached USDA mappings",
    }


def update_stats(detail: pd.DataFrame) -> None:
    stats = pd.read_csv(STATS_OUTPUT)
    stats = stats[~stats["nutrient"].eq("fvln_percent")].copy()
    additions = []
    for source, group in detail.groupby("reference_source_or_subset", sort=False):
        values = group["fvln_percent"].dropna()
        additions.append({
            "source_or_subset": source,
            "series_type": "reference",
            "profile_or_reference": "Reference",
            "value_basis": "recipe weight percentage",
            "nutrient": "fvln_percent",
            "n_recipes": int(values.size),
            "mean_value": values.mean(),
            "median_value": values.median(),
            "unit": "%",
            "notes": "RecipeWrangler-estimated FVLN share from cached ingredient weights and cached USDA food-group mappings; not source-provided reference metadata.",
        })
    pd.concat([stats, pd.DataFrame(additions)], ignore_index=True).to_csv(STATS_OUTPUT, index=False)

    if PLOT_STATS_OUTPUT.exists():
        plot_stats = pd.read_csv(PLOT_STATS_OUTPUT)
        plot_stats = plot_stats[~plot_stats["nutrient"].eq("fvln_percent")].copy()
        plot_additions = pd.DataFrame([
            {
                "reference_source_or_subset": row["source_or_subset"],
                "nutrient": "fvln_percent",
                "median_value": row["median_value"],
                "unit": "%",
                "notes": "RecipeWrangler estimate from cached ingredient weights and cached USDA food-group mappings; not source-provided reference metadata.",
            }
            for row in additions
        ])
        pd.concat([plot_stats, plot_additions], ignore_index=True).to_csv(
            PLOT_STATS_OUTPUT, index=False
        )


def main() -> None:
    canonical, labels = load_name_maps()
    profiles = load_profiles()
    allowed_healthy = healthy_reference_titles()
    profiles = profiles[
        ~profiles["source"].eq("HealthyFoods")
        | profiles["title"].map(normalize).isin(allowed_healthy)
    ].copy()
    detail = pd.DataFrame(
        calculate_row(record, canonical, labels) for record in profiles.to_dict("records")
    )
    detail.to_csv(DETAIL_OUTPUT, index=False)
    update_stats(detail)
    summary = detail.groupby("reference_source_or_subset").agg(
        recipes=("recipe_id", "size"),
        median_fvln_pct=("fvln_percent", "median"),
        mean_fvln_pct=("fvln_percent", "mean"),
        median_mapping_coverage_pct=("classified_weight_coverage_pct", "median"),
        partial_mapping_recipes=("calculation_status", lambda values: int((values != "complete").sum())),
    )
    print(summary.to_string(float_format=lambda value: f"{value:.2f}"))
    print(DETAIL_OUTPUT.name)


if __name__ == "__main__":
    main()
