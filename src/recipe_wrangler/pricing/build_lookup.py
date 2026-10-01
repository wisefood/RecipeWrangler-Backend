"""Build stage-specific and selected ingredient price lookup tables."""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from .constants import PROCESSED_DIR, STAGE_PRIORITY, TARGET_COUNTRIES, WINDOW_END
from .io import write_table


LOOKUP_GROUPS = [
    "product_normalized",
    "product_detail",
    "food_group",
    "pli_category",
    "target_country",
    "market_stage",
]


def ingredient_id(name: str, detail: str) -> str:
    value = " ".join(part for part in (name, detail) if part)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _unpack_json(values: pd.Series) -> list[str]:
    return sorted({item for value in values for item in json.loads(value)})


def _confidence(row: pd.Series) -> str:
    points = 0
    if row["n_source_countries"] >= 8:
        points += 3
    elif row["n_source_countries"] >= 3:
        points += 2
    elif row["n_source_countries"] >= 1:
        points += 1
    if row["n_observations"] >= 48:
        points += 2
    elif row["n_observations"] >= 12:
        points += 1
    if row["relative_dispersion"] <= 0.25:
        points += 2
    elif row["relative_dispersion"] <= 0.50:
        points += 1
    if row["market_stage"] in {"retail_selling", "retail_buying"}:
        points += 3
    elif row["market_stage"] in {"selling", "ex_packaging", "non_retail_buying"}:
        points += 2
    elif row["market_stage"] == "unspecified_market_price":
        points += 1
    if row["has_direct_target_observation"]:
        points += 1
    if pd.Timestamp(row["date_end"]) >= pd.Timestamp(WINDOW_END) - pd.Timedelta(days=90):
        points += 1
    if any(token in str(row["ingredient_detail"]).lower() for token in ("all types", "average")):
        points -= 1
    result = "High" if points >= 10 else "Medium" if points >= 6 else "Low"
    source_countries = json.loads(row["source_countries"])
    if source_countries == ["EU27"] and row["market_stage"] not in {
        "retail_selling", "retail_buying"
    }:
        return "Low"
    if row["market_stage"] in {"unspecified", "other_commodity"} and result == "High":
        return "Medium"
    return result


def build_stage_lookup(converted: pd.DataFrame) -> pd.DataFrame:
    def aggregate(group: pd.DataFrame) -> pd.Series:
        member_state_rows = group[group["source_country_code"] != "EU27"]
        if not member_state_rows.empty:
            group = member_state_rows
        values = group["converted_eur_per_kg"]
        countries = sorted(group["source_country_code"].unique())
        member_countries = [country for country in countries if country != "EU27"]
        iqr = values.quantile(0.75) - values.quantile(0.25)
        return pd.Series(
            {
                "estimated_eur_per_kg": values.median(),
                "mean_country_estimate": values.mean(),
                "price_dispersion": iqr,
                "relative_dispersion": iqr / values.median() if values.median() else np.nan,
                "n_source_countries": len(member_countries),
                "n_source_aggregates": int("EU27" in countries),
                "n_observations": int(group["n_observations"].sum()),
                "source_countries": json.dumps(countries),
                "source_datasets": json.dumps(_unpack_json(group["source_datasets"])),
                "date_start": min(group["date_start"]),
                "date_end": max(group["date_end"]),
                "has_direct_target_observation": bool(group["is_direct_target_observation"].any()),
                "pli_year": int(group["pli_year"].iloc[0]),
            }
        )

    stage = converted.groupby(LOOKUP_GROUPS, dropna=False).apply(
        aggregate, include_groups=False
    ).reset_index()
    stage = stage.rename(
        columns={
            "product_normalized": "ingredient_name",
            "product_detail": "ingredient_detail",
        }
    )
    stage.insert(
        0,
        "ingredient_id",
        [ingredient_id(name, detail) for name, detail in zip(stage["ingredient_name"], stage["ingredient_detail"])],
    )
    stage["confidence"] = stage.apply(_confidence, axis=1)
    return stage


def select_proxy_stages(stage_lookup: pd.DataFrame) -> pd.DataFrame:
    data = stage_lookup.copy()
    data["stage_priority"] = data["market_stage"].map(STAGE_PRIORITY).fillna(99)
    data = data.sort_values(
        ["ingredient_id", "target_country", "stage_priority", "n_source_countries", "n_observations"],
        ascending=[True, True, True, False, False],
    )
    selected = data.drop_duplicates(["ingredient_id", "target_country"], keep="first").copy()
    selected["provenance"] = selected["source_datasets"]
    columns = [
        "ingredient_id", "ingredient_name", "ingredient_detail", "food_group",
        "pli_category", "target_country", "estimated_eur_per_kg", "market_stage",
        "n_source_countries", "n_observations", "price_dispersion",
        "n_source_aggregates", "relative_dispersion", "date_start", "date_end", "confidence",
        "source_countries", "source_datasets", "provenance",
        "has_direct_target_observation", "pli_year",
    ]
    return selected[columns].sort_values(["ingredient_id", "target_country"]).reset_index(drop=True)


def _base_confidence(values: pd.Series) -> str:
    """Return the median evidence rating across a family's selected details."""

    levels = {"Low": 0, "Medium": 1, "High": 2}
    labels = {value: key for key, value in levels.items()}
    median_level = int(np.floor(values.map(levels).median()))
    return labels[median_level]


def build_base_lookup(selected: pd.DataFrame) -> pd.DataFrame:
    """Build one general median price per base product and target country."""

    def aggregate(group: pd.DataFrame) -> pd.Series:
        values = group["estimated_eur_per_kg"]
        iqr = values.quantile(0.75) - values.quantile(0.25)
        stages = sorted(group["market_stage"].unique())
        source_countries = sorted(
            {
                country
                for encoded in group["source_countries"]
                for country in json.loads(encoded)
            }
        )
        source_datasets = sorted(
            {
                dataset
                for encoded in group["source_datasets"]
                for dataset in json.loads(encoded)
            }
        )
        member_countries = [country for country in source_countries if country != "EU27"]
        return pd.Series(
            {
                "ingredient_detail": "",
                "estimated_eur_per_kg": values.median(),
                "market_stage": stages[0] if len(stages) == 1 else "mixed_selected_stages",
                "n_variants": int(group["ingredient_id"].nunique()),
                "n_source_countries": len(member_countries),
                "n_observations": int(group["n_observations"].sum()),
                "price_dispersion": iqr,
                "n_source_aggregates": int("EU27" in source_countries),
                "relative_dispersion": iqr / values.median() if values.median() else np.nan,
                "date_start": min(group["date_start"]),
                "date_end": max(group["date_end"]),
                "confidence": _base_confidence(group["confidence"]),
                "source_countries": json.dumps(source_countries),
                "source_datasets": json.dumps(source_datasets),
                "provenance": json.dumps(source_datasets),
                "has_direct_target_observation": bool(
                    group["has_direct_target_observation"].any()
                ),
                "pli_year": int(group["pli_year"].iloc[0]),
                "aggregation_method": "median_of_selected_detail_prices",
            }
        )

    grouping = ["ingredient_name", "food_group", "pli_category", "target_country"]
    base = selected.groupby(grouping, dropna=False).apply(
        aggregate, include_groups=False
    ).reset_index()
    base.insert(
        0,
        "ingredient_id",
        base["ingredient_name"].map(lambda name: ingredient_id(name, "")),
    )
    columns = [
        "ingredient_id", "ingredient_name", "ingredient_detail", "food_group",
        "pli_category", "target_country", "estimated_eur_per_kg", "market_stage",
        "n_variants", "n_source_countries", "n_observations", "price_dispersion",
        "n_source_aggregates", "relative_dispersion", "date_start", "date_end",
        "confidence", "source_countries", "source_datasets", "provenance",
        "has_direct_target_observation", "pli_year", "aggregation_method",
    ]
    result = base[columns].sort_values(["ingredient_id", "target_country"]).reset_index(drop=True)
    assert not result.duplicated(["ingredient_id", "target_country"]).any()
    return result


def build_lookup(converted: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    stages = build_stage_lookup(converted)
    selected = select_proxy_stages(stages)
    targets = selected[selected["target_country"].isin(TARGET_COUNTRIES)].copy()
    reference = selected[selected["target_country"] == "EU27"].copy()
    assert not targets.duplicated(["ingredient_id", "target_country"]).any()
    assert targets["target_country"].nunique() == len(TARGET_COUNTRIES)
    return targets, reference, stages


def main() -> None:
    converted = pd.read_csv(PROCESSED_DIR / "ec_prices_country_converted.csv")
    targets, reference, stages = build_lookup(converted)
    write_table(targets, PROCESSED_DIR / "ingredient_price_lookup.csv")
    write_table(reference, PROCESSED_DIR / "ingredient_price_reference.csv")
    write_table(stages, PROCESSED_DIR / "ingredient_price_lookup_by_stage.csv")
    base = build_base_lookup(pd.concat([targets, reference], ignore_index=True))
    base_targets = base[base["target_country"].isin(TARGET_COUNTRIES)].copy()
    base_reference = base[base["target_country"] == "EU27"].copy()
    write_table(base_targets, PROCESSED_DIR / "ingredient_price_base_lookup.csv")
    write_table(base_reference, PROCESSED_DIR / "ingredient_price_base_reference.csv")
    print(
        f"Wrote {len(targets)} detailed and {len(base_targets)} base-product "
        f"target prices"
    )


if __name__ == "__main__":
    main()
