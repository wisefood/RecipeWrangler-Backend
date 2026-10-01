"""Robust temporal aggregation without observation-rich country dominance."""

from __future__ import annotations

import json

import pandas as pd

from .constants import PROCESSED_DIR, WINDOW_END, WINDOW_START
from .io import write_table


GROUP_COLUMNS = [
    "product_normalized",
    "product_detail",
    "food_group",
    "pli_category",
    "source_country_code",
    "source_country",
    "market_stage",
]


def _json_unique(values: pd.Series) -> str:
    return json.dumps(sorted({str(value) for value in values if pd.notna(value)}))


def aggregate_country_prices(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"])
    start, end = pd.Timestamp(WINDOW_START), pd.Timestamp(WINDOW_END)
    data = data[data["date"].between(start, end)].copy()
    if data.empty:
        raise ValueError(f"No price rows in complete-month window {start.date()}..{end.date()}")

    # Multiple markets/varieties reported for the same country and date should
    # contribute one period value, not one vote per reporting market.
    daily_keys = GROUP_COLUMNS + ["date"]
    daily = (
        data.groupby(daily_keys, dropna=False, as_index=False)
        .agg(
            eur_per_kg=("eur_per_kg", "median"),
            raw_observations=("eur_per_kg", "size"),
            source_datasets=("source_dataset", _json_unique),
            source_files=("source_file", _json_unique),
            price_types=("price_type_raw", _json_unique),
        )
    )

    def aggregate(group: pd.DataFrame) -> pd.Series:
        values = group["eur_per_kg"]
        datasets = sorted({item for value in group["source_datasets"] for item in json.loads(value)})
        files = sorted({item for value in group["source_files"] for item in json.loads(value)})
        price_types = sorted({item for value in group["price_types"] for item in json.loads(value)})
        return pd.Series(
            {
                "representative_eur_per_kg": values.median(),
                "mean_eur_per_kg": values.mean(),
                "min_eur_per_kg": values.min(),
                "max_eur_per_kg": values.max(),
                "std_eur_per_kg": values.std(ddof=1),
                "iqr_eur_per_kg": values.quantile(0.75) - values.quantile(0.25),
                "n_observations": int(group["raw_observations"].sum()),
                "n_periods": len(group),
                "date_start": group["date"].min().date().isoformat(),
                "date_end": group["date"].max().date().isoformat(),
                "source_datasets": json.dumps(datasets),
                "source_files": json.dumps(files),
                "price_types": json.dumps(price_types),
            }
        )

    result = data.iloc[0:0][GROUP_COLUMNS].copy()
    result = daily.groupby(GROUP_COLUMNS, dropna=False).apply(
        aggregate, include_groups=False
    ).reset_index()
    assert not result.duplicated(GROUP_COLUMNS).any()
    assert result["representative_eur_per_kg"].gt(0).all()
    return result


def main() -> None:
    normalized = pd.read_csv(PROCESSED_DIR / "ec_prices_normalized.csv")
    aggregated = aggregate_country_prices(normalized)
    write_table(aggregated, PROCESSED_DIR / "ec_prices_country_aggregated.csv")
    print(f"Wrote {len(aggregated)} country/product/stage representatives")


if __name__ == "__main__":
    main()
