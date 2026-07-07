#!/usr/bin/env python3
"""Export per-(source, region) regional-coverage statistics from Postgres.

Coverage = the share of recipe ingredients that were sourced from the region's
OWN composition table (Irish / Hungarian) rather than the EU global pool. The
matcher pools the regional table and EU together and the best-scoring candidate
wins (no regional preference), so coverage is read directly off the per-ingredient
``source`` label, not inferred from a confidence threshold.

USDA is excluded — the analysis covers only the EU global pool and the two
regional tables. The EU region has no "own-table" concept (it IS the global pool)
so only the ``irish`` and ``hungarian`` regions are reported.

For each (dataset_source, profile_region) over the active pipeline version
(default ``recompute_2026-05-11``):

- total_recipes / total_ingredients
- regional_ingredients — ingredients sourced from the region's own table
- eu_ingredients — ingredients sourced from the EU global pool
- coverage_ingredient_pct — regional_ingredients / total_ingredients
- regional_recipes — recipes with >=1 ingredient from the regional table
- coverage_recipe_pct — regional_recipes / total_recipes

Output: ``data_to_send/viz/coverage_stats_per_source.csv``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection  # noqa: E402

load_runtime_env()

DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"
DEFAULT_OUT = REPO_ROOT / "data_to_send" / "viz" / "coverage_stats_per_source.csv"
SOURCES = ("HealthyFoods", "MyPlate", "FoodHero", "Curated Irish Recipes", "recipe1m")

# Region -> the per-ingredient `source` label of that region's own table.
REGION_TABLE = {
    "irish": "Irish Composition Table",
    "hungarian": "Hungarian Composition Table",
}


def _fetch(pipeline_version: str) -> pd.DataFrame:
    cfg = _get_config()
    query = f"""
        WITH unnested AS (
            SELECT
                p.recipe_id,
                p.source            AS dataset_source,
                p.nutrition_source  AS profile_region,
                elem->>'source'     AS pool_source
            FROM "{cfg['schema']}"."{cfg['profiles_table']}" p,
                 jsonb_array_elements(p.nutrition_profiling_details) elem
            WHERE p.pipeline_version = :pv
              AND p.source = ANY(:sources)
              AND p.nutrition_source IN ('irish','hungarian')
              AND p.nutrition_profiling_details IS NOT NULL
        ),
        per_recipe AS (
            SELECT
                dataset_source, profile_region, recipe_id,
                COUNT(*) AS n_ing,
                COUNT(*) FILTER (
                    WHERE (profile_region = 'irish'     AND pool_source = 'Irish Composition Table')
                       OR (profile_region = 'hungarian' AND pool_source = 'Hungarian Composition Table')
                ) AS n_regional
            FROM unnested
            GROUP BY 1, 2, 3
        )
        SELECT
            dataset_source,
            profile_region,
            COUNT(*)                                            AS total_recipes,
            SUM(n_ing)                                          AS total_ingredients,
            SUM(n_regional)                                     AS regional_ingredients,
            SUM(n_ing) - SUM(n_regional)                        AS eu_ingredients,
            SUM(CASE WHEN n_regional > 0 THEN 1 ELSE 0 END)     AS regional_recipes
        FROM per_recipe
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    with get_connection() as conn:
        rows = conn.execute(
            text(query), {"pv": pipeline_version, "sources": list(SOURCES)}
        ).mappings().all()
    return pd.DataFrame.from_records([dict(r) for r in rows])


def _add_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Append an ALL-datasets aggregate row per region."""
    if df.empty:
        return df
    agg = (
        df.groupby("profile_region", as_index=False)[
            ["total_recipes", "total_ingredients", "regional_ingredients",
             "eu_ingredients", "regional_recipes"]
        ]
        .sum()
    )
    agg.insert(0, "dataset_source", "ALL")
    return pd.concat([df, agg], ignore_index=True)


def _add_percentages(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in (
        "total_recipes", "total_ingredients", "regional_ingredients",
        "eu_ingredients", "regional_recipes",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["coverage_ingredient_pct"] = (
        df["regional_ingredients"] / df["total_ingredients"] * 100.0
    ).round(2)
    df["coverage_recipe_pct"] = (
        df["regional_recipes"] / df["total_recipes"] * 100.0
    ).round(2)
    return df


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    df = _fetch(args.pipeline_version)
    if df.empty:
        raise SystemExit(f"No rows found for pipeline_version={args.pipeline_version}")
    df = _add_totals(df)
    df = _add_percentages(df)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "dataset_source",
        "profile_region",
        "total_recipes",
        "total_ingredients",
        "regional_ingredients",
        "eu_ingredients",
        "coverage_ingredient_pct",
        "regional_recipes",
        "coverage_recipe_pct",
    ]
    df.to_csv(args.out, index=False, columns=columns)
    print(f"wrote {args.out.relative_to(REPO_ROOT)}  rows={len(df)}")
    print(df[columns].to_string(index=False))


if __name__ == "__main__":
    main()
