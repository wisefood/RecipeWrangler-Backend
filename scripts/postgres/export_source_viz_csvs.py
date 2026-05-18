#!/usr/bin/env python3
"""Export source-specific visualization CSVs from Postgres recipe profiles."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection  # noqa: E402

load_runtime_env()

CALCULATED_SOURCES = ("usda", "irish", "hungarian")
DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"
GROUND_TRUTH_NUTRITION_SOURCES = ("safefood", "recipe1m_original", "scraped")
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
REFERENCE_EXTRA_NUTRIENTS = ("energy_kj", "calcium_mg", "iron_mg")
MACRO_DEVIATION_NUTRIENTS = ("energy_kcal", "protein_g", "fat_g", "sugar_g")

SOURCES = {
    "safefood": {
        "db_source": "Irish_SafeFood",
        "out_dir": REPO_ROOT / "data_to_send" / "viz" / "safefood",
        "prefix": "safefood",
        "reference_source": "safefood",
        "source_order": ("safefood", *CALCULATED_SOURCES),
    },
    "healthyfoods": {
        "db_source": "HealthyFoods",
        "out_dir": REPO_ROOT / "data_to_send" / "viz" / "healthy_foods",
        "prefix": "healthyfoods",
        "reference_source": "scraped",
        "source_order": ("scraped", *CALCULATED_SOURCES),
    },
    "foodhero": {
        "db_source": "FoodHero",
        "out_dir": REPO_ROOT / "data_to_send" / "viz" / "foodhero",
        "prefix": "foodhero",
        "reference_source": None,
        "source_order": CALCULATED_SOURCES,
    },
    "myplate": {
        "db_source": "MyPlate",
        "out_dir": REPO_ROOT / "data_to_send" / "viz" / "myplate",
        "prefix": "myplate",
        "reference_source": None,
        "source_order": CALCULATED_SOURCES,
    },
    "recipe1m": {
        "db_source": "recipe1m",
        "out_dir": REPO_ROOT / "data_to_send" / "viz" / "recipe1m",
        "prefix": "recipe1m",
        "reference_source": "recipe1m_original",
        "source_order": ("recipe1m_original", *CALCULATED_SOURCES),
    },
}

HEALTHYFOODS_NUTRITION = (
    REPO_ROOT / "data" / "HealthyFoods" / "HealthyFood_recipes_nutrition.json"
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        if not match:
            return None
        try:
            number = float(match.group(0))
        except ValueError:
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


def _fetch_profiles(db_source: str, basis: str, pipeline_version: str) -> pd.DataFrame:
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
          AND (
              pipeline_version = :pv
              OR nutrition_source = ANY(:gt)
          )
        ORDER BY title, nutrition_source
    """
    with get_connection() as conn:
        rows = conn.execute(
            text(query),
            {"source": db_source, "pv": pipeline_version, "gt": list(GROUND_TRUTH_NUTRITION_SOURCES)},
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


def _fetch_healthyfoods_serving_weights(pipeline_version: str) -> dict[str, float]:
    """Return {title: serving_weight_g} from HealthyFoods pipeline rows in Postgres.

    Serving weight is estimated as total ingredient weight (sum of weight_g from
    nutrition_profiling_details) divided by serves (from the trace).
    Uses the usda profile row as the reference since ingredient weights are
    identical across regions.
    """
    cfg = _get_config()
    query = f"""
        SELECT
            title,
            nutrition_profiling_details,
            trace->'profile_result'->>'serves' AS serves_str,
            trace->'healthyfoods_recipe'->>'serves' AS hf_serves_str
        FROM "{cfg['schema']}"."{cfg['profiles_table']}"
        WHERE source = 'HealthyFoods' AND nutrition_source = 'usda'
          AND pipeline_version = :pv
          AND nutrition_profiling_details IS NOT NULL
    """
    serving_weights: dict[str, float] = {}
    try:
        with get_connection() as conn:
            rows = conn.execute(text(query), {"pv": pipeline_version}).mappings().all()
        for row in rows:
            title = row["title"]
            if not title:
                continue
            # Resolve serves
            serves = _to_float(row["serves_str"]) or _to_float(row["hf_serves_str"]) or 1.0
            if serves <= 0:
                serves = 1.0
            # Sum ingredient weights
            details = row["nutrition_profiling_details"]
            if not isinstance(details, list):
                try:
                    details = json.loads(details) if isinstance(details, str) else None
                except Exception:
                    details = None
            if not isinstance(details, list):
                continue
            total_w = sum(
                float(d.get("weight_g") or 0)
                for d in details
                if isinstance(d, dict)
            )
            if total_w > 0:
                serving_weights[title] = total_w / serves
    except Exception:
        pass
    return serving_weights


def _compute_scraped_nutri_label(
    record: dict[str, Any],
    serving_weight_g: float | None,
) -> str | None:
    """Compute Nutri-Score grade from per-serving scraped values + serving weight."""
    if not serving_weight_g or serving_weight_g <= 0:
        return None
    energy_kj = _to_float(record.get("energy_kj"))
    energy_kcal = _to_float(record.get("energy_kcal"))
    if energy_kj is None and energy_kcal is not None:
        energy_kj = energy_kcal * 4.184
    
    sugar = _to_float(record.get("sugar_g"))
    sat_fat = _to_float(record.get("saturated_fat_g"))
    sodium = _to_float(record.get("sodium_mg"))
    fibre = _to_float(record.get("fibre_g"))
    protein = _to_float(record.get("protein_g"))
    if any(v is None for v in (energy_kj, sugar, sat_fat, sodium, fibre, protein)):
        return None
    try:
        from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
        nutrient_values = {
            "energy": energy_kj / serving_weight_g * 100.0,
            "sugar": sugar / serving_weight_g * 100.0,
            "saturated_fats": sat_fat / serving_weight_g * 100.0,
            "sodium": sodium / serving_weight_g * 100.0,
            "fibers": fibre / serving_weight_g * 100.0,
            "proteins": protein / serving_weight_g * 100.0,
            "fruit_percentage": 0.0,
        }
        result = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
        raw = result.get("nutri_score", "")
        return str(raw).replace("Nutriscore_", "").strip() or None
    except Exception:
        return None


def _load_healthyfoods_reference(pipeline_version: str) -> pd.DataFrame:
    payload = json.loads(HEALTHYFOODS_NUTRITION.read_text(encoding="utf-8"))
    serving_weights = _fetch_healthyfoods_serving_weights(pipeline_version)
    rows = []
    field_map = {
        "Calories": "energy_kcal",
        "Kilojoules": "energy_kj",
        "Protein": "protein_g",
        "Total fat": "fat_g",
        "Saturated fat": "saturated_fat_g",
        "Carbohydrates": "carbohydrate_g",
        "Sugar": "sugar_g",
        "Dietary fibre": "fibre_g",
        "Sodium": "sodium_mg",
        "Calcium": "calcium_mg",
        "Iron": "iron_mg",
    }
    for recipe in payload.get("recipes", []):
        if not isinstance(recipe, dict):
            continue
        nutrition = recipe.get("nutrition_per_serve") or {}
        title = recipe.get("title")
        record: dict[str, Any] = {
            "recipe_id": None,
            "title": title,
            "url": recipe.get("url"),
            "source": "scraped",
            "nutri_label": None,
        }
        for source_key, out_key in field_map.items():
            record[out_key] = _to_float(nutrition.get(source_key))
        if record.get("energy_kcal") is not None:
            sw = serving_weights.get(title) if title else None
            record["nutri_label"] = _compute_scraped_nutri_label(record, sw)
            rows.append(record)
    return pd.DataFrame.from_records(rows)


def _add_reference_rows(key: str, df: pd.DataFrame, pipeline_version: str) -> pd.DataFrame:
    if key != "healthyfoods":
        return df
    reference = _load_healthyfoods_reference(pipeline_version)
    if reference.empty:
        return df
    for col in df.columns:
        if col not in reference.columns:
            reference[col] = None
    for col in reference.columns:
        if col not in df.columns:
            df[col] = None
    return pd.concat([reference[df.columns], df], ignore_index=True)


def _source_order(df: pd.DataFrame, order: tuple[str, ...]) -> list[str]:
    present = set(df["source"].dropna().astype(str))
    ordered = [source for source in order if source in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


def _write_nutrition_flat(df: pd.DataFrame, out_dir: Path, prefix: str) -> Path:
    path = out_dir / f"{prefix}_nutrition_flat.csv"
    columns = [
        "recipe_id",
        "title",
        "source",
        *NUTRIENTS,
        "nutri_label",
    ]
    if "url" in df.columns:
        columns.insert(2, "url")
    df.sort_values(["title", "source"]).to_csv(path, index=False, columns=columns)
    return path


def _write_reference_summary(
    df: pd.DataFrame,
    out_dir: Path,
    prefix: str,
    reference_source: str | None,
) -> Path:
    path = out_dir / f"{prefix}_macros_summary.csv"
    source_df = df[df["source"] == reference_source] if reference_source else df
    rows = []
    for nutrient in NUTRIENTS:
        values = source_df[nutrient].dropna()
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


def _write_mean_median(
    df: pd.DataFrame,
    out_dir: Path,
    prefix: str,
    source_order: tuple[str, ...],
) -> Path:
    path = out_dir / f"{prefix}_macros_mean_median_per_source.csv"
    rows = []
    for source in _source_order(df, source_order):
        source_df = df[df["source"] == source]
        row: dict[str, Any] = {"source": source}
        for nutrient in MACRO_DEVIATION_NUTRIENTS:
            values = source_df[nutrient].dropna()
            row[f"{nutrient}_mean"] = round(values.mean(), 2) if len(values) else None
            row[f"{nutrient}_median"] = round(values.median(), 2) if len(values) else None
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _deviation_detail(
    df: pd.DataFrame,
    reference_source: str,
    calculated_sources: tuple[str, ...],
) -> pd.DataFrame:
    reference = df[df["source"] == reference_source].copy()
    calculated = df[df["source"].isin(calculated_sources)].copy()
    join_key = "recipe_id"
    if reference["recipe_id"].isna().all():
        join_key = "title"
    reference_columns = [*MACRO_DEVIATION_NUTRIENTS]
    if join_key != "title":
        reference_columns.insert(0, "title")
    reference = reference.set_index(join_key)[reference_columns]
    reference = reference.rename(
        columns={n: f"{n}_{reference_source}" for n in MACRO_DEVIATION_NUTRIENTS}
    )

    detail_rows: list[dict[str, Any]] = []
    for source in calculated_sources:
        source_df = calculated[calculated["source"] == source].set_index(join_key)
        joined = source_df.join(reference, how="inner", rsuffix="_ref")
        for recipe_key, row in joined.iterrows():
            detail: dict[str, Any] = {
                join_key: recipe_key,
                "title": (
                    recipe_key
                    if join_key == "title"
                    else row.get("title_ref") or row.get("title")
                ),
                "source": source,
            }
            if join_key == "title":
                detail["recipe_id"] = row.get("recipe_id")
            for nutrient in MACRO_DEVIATION_NUTRIENTS:
                calculated_value = _to_float(row.get(nutrient))
                reference_value = _to_float(row.get(f"{nutrient}_{reference_source}"))
                detail[f"{nutrient}_calculated"] = calculated_value
                detail[f"{nutrient}_{reference_source}"] = reference_value
                pct = _pct_deviation(calculated_value, reference_value)
                detail[f"{nutrient}_pct_dev"] = round(pct, 2) if pct is not None else None
            detail_rows.append(detail)
    return pd.DataFrame.from_records(detail_rows)


def _write_deviation_summary(
    detail: pd.DataFrame,
    out_dir: Path,
    prefix: str,
    calculated_sources: tuple[str, ...],
    reference_label: str,
) -> Path:
    path = out_dir / f"{prefix}_macros_pct_deviation_from_{reference_label}.csv"
    rows = []
    for source in calculated_sources:
        source_df = detail[detail["source"] == source]
        row: dict[str, Any] = {"source": source}
        for nutrient in MACRO_DEVIATION_NUTRIENTS:
            values = source_df[f"{nutrient}_pct_dev"].dropna()
            row[f"{nutrient}_median_pct_dev"] = round(values.median(), 2) if len(values) else None
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _write_deviation_detail(detail: pd.DataFrame, out_dir: Path, prefix: str) -> Path:
    path = out_dir / f"{prefix}_macros_pct_deviation_by_recipe.csv"
    if detail.empty:
        detail.to_csv(path, index=False)
    else:
        detail.sort_values(["title", "source"]).to_csv(path, index=False)
    return path


def _write_nutriscore_distribution(
    df: pd.DataFrame,
    out_dir: Path,
    prefix: str,
    source_order: tuple[str, ...],
) -> Path:
    path = out_dir / f"{prefix}_nutriscore_distribution_per_source.csv"
    rows = []
    for source in _source_order(df, source_order):
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


def export_source(key: str, basis: str, pipeline_version: str) -> list[Path]:
    cfg = SOURCES[key]
    out_dir: Path = cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    df = _fetch_profiles(cfg["db_source"], basis, pipeline_version)
    if df.empty:
        raise RuntimeError(f"No Postgres profiles found for {cfg['db_source']}.")
    df = _add_reference_rows(key, df, pipeline_version)

    prefix = cfg["prefix"]
    reference_source = cfg["reference_source"]
    source_order = cfg["source_order"]
    paths = [
        _write_nutrition_flat(df, out_dir, prefix),
        _write_reference_summary(df, out_dir, prefix, reference_source),
        _write_mean_median(df, out_dir, prefix, source_order),
        _write_nutriscore_distribution(
            df,
            out_dir,
            prefix,
            source_order,
        ),
    ]
    if reference_source:
        reference_label = "groundtruth" if reference_source in {"safefood", "scraped"} else reference_source
        detail = _deviation_detail(df, reference_source, CALCULATED_SOURCES)
        paths.append(
            _write_deviation_summary(
                detail,
                out_dir,
                prefix,
                CALCULATED_SOURCES,
                reference_label,
            )
        )
        paths.append(_write_deviation_detail(detail, out_dir, prefix))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", choices=sorted(SOURCES), default=sorted(SOURCES))
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

    all_paths: list[Path] = []
    for source in args.sources:
        paths = export_source(source, args.basis, args.pipeline_version)
        all_paths.extend(paths)
        print(f"{source}: wrote {len(paths)} CSVs")
        for path in paths:
            print(f"  {path.relative_to(REPO_ROOT)}")
    print(f"total_csvs={len(all_paths)}")


if __name__ == "__main__":
    main()
