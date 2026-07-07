#!/usr/bin/env python3
"""Export SafeFood visualization CSVs from Postgres recipe profiles."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection  # noqa: E402

DEFAULT_OUT_DIR = REPO_ROOT / "data_to_send" / "viz" / "safefood"
DATASET_SOURCE = "Curated Irish Recipes"
REFERENCE_SOURCE = "safefood_rcsi"
CALCULATED_SOURCES = ("eu", "irish", "hungarian")
DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"

NUTRIENTS = (
    "energy_kcal",
    "protein_g",
    "fat_g",
    "saturated_fat_g",
    "carbohydrate_g",
    "sugar_g",
    "fibre_g",
    "sodium_mg",
)
MACRO_DEVIATION_NUTRIENTS = ("energy_kcal", "protein_g", "fat_g", "sugar_g")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _nutri_label(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    label = value.get("nutri_score") or value.get("label")
    if not label:
        return None
    return str(label).replace("Nutriscore_", "").strip() or None


def _pct_deviation(calculated: float | None, reference: float | None) -> float | None:
    if calculated is None or reference is None:
        return None
    if reference == 0:
        return 0.0 if calculated == 0 else None
    return abs((calculated - reference) / reference) * 100.0


def _fetch_profiles(basis: str, pipeline_version: str) -> pd.DataFrame:
    cfg = _get_config()
    nutrient_column = (
        "total_nutrients_per_serving" if basis == "per_serving" else "total_nutrients"
    )
    query = f"""
        SELECT
            recipe_id,
            title,
            nutrition_source,
            {nutrient_column} AS nutrients,
            nutri_score
        FROM "{cfg['schema']}"."{cfg['profiles_table']}"
        WHERE source = :source
          AND nutrition_source = ANY(:nutrition_sources)
          AND (pipeline_version = :pv OR nutrition_source = :ref)
        ORDER BY title, nutrition_source
    """
    with get_connection() as conn:
        rows = conn.execute(
            text(query),
            {
                "source": DATASET_SOURCE,
                "nutrition_sources": [REFERENCE_SOURCE, *CALCULATED_SOURCES],
                "pv": pipeline_version,
                "ref": REFERENCE_SOURCE,
            },
        ).mappings().all()

    records: list[dict[str, Any]] = []
    for row in rows:
        nutrients = row["nutrients"] or {}
        record = {
            "recipe_id": row["recipe_id"],
            "title": row["title"],
            "source": row["nutrition_source"],
            "nutri_label": _nutri_label(row["nutri_score"]),
        }
        for nutrient in NUTRIENTS:
            record[nutrient] = _to_float(nutrients.get(nutrient))
        records.append(record)
    return pd.DataFrame.from_records(records)


def _write_nutrition_flat(df: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_nutrition_flat.csv"
    columns = ["recipe_id", "title", "source", *NUTRIENTS, "nutri_label"]
    df.sort_values(["title", "source"]).to_csv(path, index=False, columns=columns)
    return path


def _write_reference_summary(df: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_macros_summary.csv"
    reference = df[df["source"] == REFERENCE_SOURCE]
    rows = []
    for nutrient in NUTRIENTS:
        values = reference[nutrient].dropna()
        rows.append(
            {
                "nutrient": nutrient,
                "mean": round(values.mean(), 2) if len(values) else None,
                "median": round(values.median(), 2) if len(values) else None,
                "min": round(values.min(), 2) if len(values) else None,
                "max": round(values.max(), 2) if len(values) else None,
                "count": int(values.count()),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _write_mean_median(df: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_macros_mean_median_per_source.csv"
    source_order = [REFERENCE_SOURCE, *CALCULATED_SOURCES]
    rows = []
    for source in source_order:
        source_df = df[df["source"] == source]
        row: dict[str, Any] = {"source": source}
        for nutrient in MACRO_DEVIATION_NUTRIENTS:
            values = source_df[nutrient].dropna()
            row[f"{nutrient}_mean"] = round(values.mean(), 2) if len(values) else None
            row[f"{nutrient}_median"] = round(values.median(), 2) if len(values) else None
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _deviation_detail(df: pd.DataFrame) -> pd.DataFrame:
    reference = (
        df[df["source"] == REFERENCE_SOURCE]
        .set_index("recipe_id")[["title", *MACRO_DEVIATION_NUTRIENTS]]
        .rename(columns={n: f"{n}_safefood" for n in MACRO_DEVIATION_NUTRIENTS})
    )
    detail_rows: list[dict[str, Any]] = []
    for source in CALCULATED_SOURCES:
        source_df = df[df["source"] == source].set_index("recipe_id")
        joined = source_df.join(reference, how="inner", rsuffix="_ref")
        for recipe_id, row in joined.iterrows():
            detail: dict[str, Any] = {
                "recipe_id": recipe_id,
                "title": row.get("title_safefood") or row.get("title"),
                "source": source,
            }
            for nutrient in MACRO_DEVIATION_NUTRIENTS:
                calculated = _to_float(row.get(nutrient))
                safefood = _to_float(row.get(f"{nutrient}_safefood"))
                detail[f"{nutrient}_calculated"] = calculated
                detail[f"{nutrient}_safefood"] = safefood
                pct = _pct_deviation(calculated, safefood)
                detail[f"{nutrient}_pct_dev"] = round(pct, 2) if pct is not None else None
            detail_rows.append(detail)
    return pd.DataFrame.from_records(detail_rows)


def _write_deviation_summary(detail: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_macros_pct_deviation_from_groundtruth.csv"
    rows = []
    for source in CALCULATED_SOURCES:
        source_df = detail[detail["source"] == source]
        row: dict[str, Any] = {"source": source}
        for nutrient in MACRO_DEVIATION_NUTRIENTS:
            values = source_df[f"{nutrient}_pct_dev"].dropna()
            row[f"{nutrient}_median_pct_dev"] = (
                round(values.median(), 2) if len(values) else None
            )
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _write_deviation_detail(detail: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_macros_pct_deviation_by_recipe.csv"
    detail.sort_values(["title", "source"]).to_csv(path, index=False)
    return path


def _write_nutriscore_distribution(df: pd.DataFrame, out_dir: Path) -> Path:
    path = out_dir / "safefood_nutriscore_distribution_per_source.csv"
    rows = []
    for source in (REFERENCE_SOURCE, *CALCULATED_SOURCES):
        source_df = df[(df["source"] == source) & df["nutri_label"].notna()]
        total = int(len(source_df))
        counts = source_df["nutri_label"].value_counts()
        for label in ("A", "B", "C", "D", "E"):
            count = int(counts.get(label, 0))
            rows.append(
                {
                    "source": source,
                    "nutri_label": label,
                    "count": count,
                    "total": total,
                    "pct": round((count / total) * 100.0, 1) if total else 0.0,
                }
            )
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def export_safefood_viz_csvs(out_dir: Path, basis: str, pipeline_version: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = _fetch_profiles(basis, pipeline_version)
    if df.empty:
        raise RuntimeError(f"No {DATASET_SOURCE} profiles found in Postgres.")

    expected_counts = df.groupby("source")["recipe_id"].nunique().to_dict()
    missing_sources = [s for s in [REFERENCE_SOURCE, *CALCULATED_SOURCES] if s not in expected_counts]
    if missing_sources:
        raise RuntimeError(f"Missing nutrition sources: {', '.join(missing_sources)}")

    detail = _deviation_detail(df)
    return [
        _write_nutrition_flat(df, out_dir),
        _write_reference_summary(df, out_dir),
        _write_mean_median(df, out_dir),
        _write_deviation_summary(detail, out_dir),
        _write_deviation_detail(detail, out_dir),
        _write_nutriscore_distribution(df, out_dir),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--basis",
        choices=("per_serving", "total"),
        default="per_serving",
        help="Use per-serving or whole-recipe nutrient values.",
    )
    parser.add_argument(
        "--pipeline-version",
        default=DEFAULT_PIPELINE_VERSION,
        help="Restrict calculated rows to this pipeline_version (ground-truth rows pass through).",
    )
    args = parser.parse_args()

    paths = export_safefood_viz_csvs(args.out_dir, args.basis, args.pipeline_version)
    print(f"Wrote {len(paths)} CSVs to {args.out_dir}")
    for path in paths:
        print(path.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
