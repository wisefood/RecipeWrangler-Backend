#!/usr/bin/env python3
"""Export per-(source, region) nutrition fallback statistics from Postgres.

For each (dataset_source, profile_region) computes, over the active pipeline
version (default `recompute_2026-05-11`):

- total_recipes / total_ingredients
- pool_fallback_recipes / pool_fallback_ingredients — recipes whose Irish or
  Hungarian region profile fell back to the USDA composition pool for at least
  one ingredient (computed from per-ingredient ``source == 'USDA Nutrients'``).
  Always zero for the ``usda`` region itself (no cross-pool concept).
- low_confidence_recipes / low_confidence_ingredients — at least one ingredient
  with ``match_confidence`` in {``weak``, ``none``}.

Output: ``data_to_send/viz/fallback_stats_per_source.csv``.
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
DEFAULT_OUT = REPO_ROOT / "data_to_send" / "viz" / "fallback_stats_per_source.csv"
SOURCES = ("HealthyFoods", "MyPlate", "FoodHero", "Irish_SafeFood", "recipe1m")


def _fetch(pipeline_version: str) -> pd.DataFrame:
    cfg = _get_config()
    query = f"""
        WITH unnested AS (
            SELECT
                p.recipe_id,
                p.source            AS dataset_source,
                p.nutrition_source  AS profile_region,
                elem->>'source'             AS pool_source,
                elem->>'match_confidence'   AS conf
            FROM "{cfg['schema']}"."{cfg['profiles_table']}" p,
                 jsonb_array_elements(p.nutrition_profiling_details) elem
            WHERE p.pipeline_version = :pv
              AND p.source = ANY(:sources)
              AND p.nutrition_profiling_details IS NOT NULL
        ),
        per_recipe AS (
            SELECT
                dataset_source, profile_region, recipe_id,
                COUNT(*)                                              AS n_ing,
                COUNT(*) FILTER (WHERE pool_source = 'USDA Nutrients') AS n_pool_usda,
                COUNT(*) FILTER (WHERE conf IN ('weak','none'))        AS n_low_conf
            FROM unnested
            GROUP BY 1, 2, 3
        )
        SELECT
            dataset_source,
            profile_region,
            COUNT(*) AS total_recipes,
            SUM(n_ing) AS total_ingredients,
            SUM(CASE WHEN profile_region IN ('irish','hungarian') AND n_pool_usda > 0 THEN 1 ELSE 0 END)
                AS pool_fallback_recipes,
            SUM(CASE WHEN profile_region IN ('irish','hungarian') THEN n_pool_usda ELSE 0 END)
                AS pool_fallback_ingredients,
            SUM(CASE WHEN n_low_conf > 0 THEN 1 ELSE 0 END) AS low_confidence_recipes,
            SUM(n_low_conf) AS low_confidence_ingredients
        FROM per_recipe
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    with get_connection() as conn:
        rows = conn.execute(
            text(query), {"pv": pipeline_version, "sources": list(SOURCES)}
        ).mappings().all()
    return pd.DataFrame.from_records([dict(r) for r in rows])


def _add_percentages(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in (
        "total_recipes",
        "total_ingredients",
        "pool_fallback_recipes",
        "pool_fallback_ingredients",
        "low_confidence_recipes",
        "low_confidence_ingredients",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["pool_fallback_pct"] = (
        df["pool_fallback_recipes"] / df["total_recipes"] * 100.0
    ).round(2)
    df["low_confidence_pct"] = (
        df["low_confidence_recipes"] / df["total_recipes"] * 100.0
    ).round(2)
    df["pool_fallback_ingredient_pct"] = (
        df["pool_fallback_ingredients"] / df["total_ingredients"] * 100.0
    ).round(2)
    df["low_confidence_ingredient_pct"] = (
        df["low_confidence_ingredients"] / df["total_ingredients"] * 100.0
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
    df = _add_percentages(df)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "dataset_source",
        "profile_region",
        "total_recipes",
        "total_ingredients",
        "pool_fallback_recipes",
        "pool_fallback_pct",
        "pool_fallback_ingredients",
        "pool_fallback_ingredient_pct",
        "low_confidence_recipes",
        "low_confidence_pct",
        "low_confidence_ingredients",
        "low_confidence_ingredient_pct",
    ]
    df.to_csv(args.out, index=False, columns=columns)
    print(f"wrote {args.out.relative_to(REPO_ROOT)}  rows={len(df)}")
    print(df[columns].to_string(index=False))


if __name__ == "__main__":
    main()
