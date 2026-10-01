"""Explainable ingredient cost tiers calibrated from EU base products."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .constants import PROCESSED_DIR, TARGET_COUNTRIES
from .io import write_table


@dataclass(frozen=True)
class CostClassificationConfig:
    """Versioned constants for the deterministic cost heuristic."""

    cost_reference_version: str = "EU-2026-v1"
    calibration_date: str = "2026-08-28"
    global_quantiles: tuple[float, float] = (1 / 3, 2 / 3)
    quantile_method: str = "linear"
    minimum_category_size_normal: int = 7
    minimum_category_size_coarse: int = 4
    detail_substantially_lower: float = 0.80
    detail_lower: float = 0.90
    detail_typical_upper: float = 1.10
    detail_higher_upper: float = 1.25


DEFAULT_COST_CONFIG = CostClassificationConfig()
EU_PRICE_COLUMN = "estimated_eur_per_kg"
GLOBAL_TIERS = ("€", "€€", "€€€")
COUNTRY_PRICE_COLUMNS = {
    "IE": "price_ie_eur_kg",
    "HU": "price_hu_eur_kg",
    "SI": "price_si_eur_kg",
}


def _valid_prices(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric[numeric.notna() & np.isfinite(numeric) & numeric.gt(0)]


def compute_global_thresholds(
    base_reference: pd.DataFrame,
    config: CostClassificationConfig = DEFAULT_COST_CONFIG,
    *,
    price_column: str = EU_PRICE_COLUMN,
) -> tuple[float, float]:
    """Calculate frozen tertile thresholds from valid EU base prices only."""

    prices = _valid_prices(base_reference[price_column])
    if len(prices) < 3:
        raise ValueError("At least three valid EU base-product prices are required")
    thresholds = np.quantile(
        prices.to_numpy(dtype=float),
        config.global_quantiles,
        method=config.quantile_method,
    )
    if thresholds[0] >= thresholds[1]:
        raise ValueError(f"Non-increasing global thresholds: {thresholds}")
    return float(thresholds[0]), float(thresholds[1])


def empirical_base_percentiles(values: pd.Series) -> pd.Series:
    """Return average-rank empirical percentiles `(rank - 0.5) / N`."""

    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    result = pd.Series(np.nan, index=values.index, dtype=float)
    count = int(valid.sum())
    if count:
        ranks = numeric[valid].rank(method="average", ascending=True)
        result.loc[valid] = (ranks - 0.5) / count
    return result


def percentiles_against_base_reference(
    values: pd.Series,
    base_prices: pd.Series,
) -> pd.Series:
    """Position arbitrary prices in the empirical EU base-product distribution."""

    reference = np.sort(_valid_prices(base_prices).to_numpy(dtype=float))
    if not len(reference):
        raise ValueError("No valid base-product reference prices")
    numeric = pd.to_numeric(values, errors="coerce")
    result = pd.Series(np.nan, index=values.index, dtype=float)
    for index, value in numeric.items():
        if pd.isna(value) or not np.isfinite(value) or value <= 0:
            continue
        left = np.searchsorted(reference, value, side="left")
        right = np.searchsorted(reference, value, side="right")
        result.at[index] = (left + 0.5 * (right - left)) / len(reference)
    return result


def assign_global_cost_tier(
    prices: pd.Series,
    thresholds: tuple[float, float],
) -> pd.Series:
    """Apply frozen EU thresholds without recalibrating from the input rows."""

    lower, upper = thresholds
    numeric = pd.to_numeric(prices, errors="coerce")
    result = pd.Series(pd.NA, index=prices.index, dtype="object")
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    result.loc[valid & numeric.lt(lower)] = "€"
    result.loc[valid & numeric.ge(lower) & numeric.lt(upper)] = "€€"
    result.loc[valid & numeric.ge(upper)] = "€€€"
    return result


def compute_category_positions(
    base_reference: pd.DataFrame,
    config: CostClassificationConfig = DEFAULT_COST_CONFIG,
    *,
    price_column: str = EU_PRICE_COLUMN,
) -> pd.DataFrame:
    """Rank base products within category, applying explicit sparse-group rules."""

    required = {"ingredient_id", "ingredient_name", "food_group", price_column}
    missing = required.difference(base_reference.columns)
    if missing:
        raise ValueError(f"Missing category-position columns: {sorted(missing)}")
    rows: list[dict[str, Any]] = []
    for category, group in base_reference.groupby("food_group", dropna=False, sort=True):
        prices = pd.to_numeric(group[price_column], errors="coerce")
        valid = prices.notna() & np.isfinite(prices) & prices.gt(0)
        category_size = int(valid.sum())
        if category_size >= config.minimum_category_size_normal:
            resolution = "normal"
        elif category_size >= config.minimum_category_size_coarse:
            resolution = "coarse"
        elif category_size >= 2:
            resolution = "insufficient_for_band"
        else:
            resolution = "unavailable"
        ranks = prices[valid].rank(method="average", ascending=True)
        for index, row in group.iterrows():
            rank = float(ranks.at[index]) if index in ranks.index else np.nan
            percentile = (
                (rank - 0.5) / category_size
                if category_size >= 2 and np.isfinite(rank)
                else np.nan
            )
            position: str | None = None
            if category_size >= config.minimum_category_size_coarse and np.isfinite(percentile):
                if percentile < 1 / 3:
                    position = "Low"
                elif percentile < 2 / 3:
                    position = "Middle"
                else:
                    position = "High"
            rows.append(
                {
                    "ingredient_id": row["ingredient_id"],
                    "ingredient_name": row["ingredient_name"],
                    "food_group": category,
                    "within_category_rank": rank,
                    "within_category_percentile": percentile,
                    "within_category_position": position,
                    "within_category_resolution": resolution,
                    "category_size": category_size,
                }
            )
    return pd.DataFrame(rows)


def describe_detail_to_base(
    ratio: float | None,
    config: CostClassificationConfig = DEFAULT_COST_CONFIG,
) -> str | None:
    """Describe a detail/base price ratio without changing either cost tier."""

    if ratio is None or pd.isna(ratio) or not np.isfinite(ratio) or ratio <= 0:
        return None
    if ratio < config.detail_substantially_lower:
        return "substantially_lower"
    if ratio < config.detail_lower:
        return "lower"
    if ratio <= config.detail_typical_upper:
        return "typical"
    if ratio <= config.detail_higher_upper:
        return "higher"
    return "substantially_higher"


def compute_tier_boundary_margin(
    prices: pd.Series,
    thresholds: tuple[float, float],
) -> pd.DataFrame:
    """Measure proportional distance from the nearest global-tier boundary."""

    numeric = pd.to_numeric(prices, errors="coerce")
    lower, upper = thresholds
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    distance = pd.Series(np.nan, index=prices.index, dtype=float)
    distance.loc[valid] = np.minimum(
        np.abs(np.log(numeric.loc[valid]) - np.log(lower)),
        np.abs(np.log(numeric.loc[valid]) - np.log(upper)),
    )
    margin = np.exp(distance) - 1
    return pd.DataFrame(
        {
            "tier_boundary_log_distance": distance,
            "tier_boundary_margin": margin,
            "tier_boundary_margin_pct": 100 * margin,
        },
        index=prices.index,
    )


def compute_pairwise_cost_saving(
    original_price_eur_kg: float,
    substitute_price_eur_kg: float,
) -> dict[str, float]:
    """Return directional savings for an already-valid substitute pair."""

    if original_price_eur_kg <= 0 or substitute_price_eur_kg <= 0:
        raise ValueError("Pairwise price comparison requires positive prices")
    relative_saving = (
        original_price_eur_kg - substitute_price_eur_kg
    ) / original_price_eur_kg
    return {
        "relative_saving": relative_saving,
        "relative_saving_pct": 100 * relative_saving,
        "log_price_ratio": float(
            np.log(original_price_eur_kg / substitute_price_eur_kg)
        ),
    }


def calculate_recipe_cost(
    ingredients: pd.DataFrame,
    servings: int,
    *,
    quantity_column: str = "ingredient_quantity_kg",
    price_column: str = "ingredient_price_eur_kg",
) -> dict[str, float]:
    """Calculate recipe cost without assigning an unsupported recipe cost tier."""

    if servings <= 0:
        raise ValueError("servings must be positive")
    quantities = pd.to_numeric(ingredients[quantity_column], errors="coerce")
    prices = pd.to_numeric(ingredients[price_column], errors="coerce")
    if quantities.isna().any() or prices.isna().any():
        raise ValueError("Recipe costing does not silently fill missing quantities or prices")
    if quantities.lt(0).any() or prices.le(0).any():
        raise ValueError("Recipe quantities must be non-negative and prices positive")
    total = float((quantities * prices).sum())
    return {
        "recipe_cost_total_eur": total,
        "recipe_cost_per_serving_eur": total / servings,
    }


def _country_price_table(lookup: pd.DataFrame) -> pd.DataFrame:
    required_countries = set(TARGET_COUNTRIES)
    observed = set(lookup["target_country"].dropna().unique())
    if not required_countries.issubset(observed):
        raise ValueError(
            f"Missing target countries: {sorted(required_countries - observed)}"
        )
    wide = lookup.pivot(
        index="ingredient_id",
        columns="target_country",
        values="estimated_eur_per_kg",
    ).rename(columns=COUNTRY_PRICE_COLUMNS)
    return wide.reset_index()


def _evidence_columns(reference: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(index=reference.index)
    result["price_evidence_confidence"] = reference["confidence"]
    result["observation_count"] = reference["n_observations"]
    result["source_country_count"] = reference["n_source_countries"]
    result["price_dispersion"] = reference["price_dispersion"]
    result["relative_price_dispersion"] = reference["relative_dispersion"]
    result["source_market_stage"] = reference["market_stage"]
    result["source_countries"] = reference["source_countries"]
    result["source_datasets"] = reference["source_datasets"]
    result["provenance"] = reference["provenance"]
    result["reference_date_start"] = reference["date_start"]
    result["reference_date_end"] = reference["date_end"]
    result["reference_date"] = (
        reference["date_start"].astype(str) + " to " + reference["date_end"].astype(str)
    )
    return result


def build_enriched_cost_dataset(
    base_reference: pd.DataFrame,
    base_lookup: pd.DataFrame,
    detail_reference: pd.DataFrame,
    detail_lookup: pd.DataFrame,
    config: CostClassificationConfig = DEFAULT_COST_CONFIG,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Create one classified row per base product and detailed product form."""

    if base_reference["ingredient_id"].duplicated().any():
        raise ValueError("Base reference contains duplicate ingredient IDs")
    if detail_reference["ingredient_id"].duplicated().any():
        raise ValueError("Detail reference contains duplicate ingredient IDs")
    thresholds = compute_global_thresholds(base_reference, config)
    category = compute_category_positions(base_reference, config).set_index(
        "ingredient_id"
    )
    base_prices = base_reference.set_index("ingredient_name")[EU_PRICE_COLUMN]
    base_ids = base_reference.set_index("ingredient_name")["ingredient_id"]

    def level_frame(
        reference: pd.DataFrame,
        lookup: pd.DataFrame,
        product_level: str,
    ) -> pd.DataFrame:
        result = pd.DataFrame(index=reference.index)
        result["source_ingredient_id"] = reference["ingredient_id"]
        result["product_id"] = product_level + "__" + reference["ingredient_id"]
        result["canonical_name"] = reference["ingredient_name"]
        result["product_detail"] = reference["ingredient_detail"].fillna("")
        result["product_level"] = product_level
        result["food_category"] = reference["food_group"]
        result["pli_category"] = reference["pli_category"]
        result["eu_reference_price_eur_kg"] = reference[EU_PRICE_COLUMN]
        countries = _country_price_table(lookup).set_index("ingredient_id")
        for column in COUNTRY_PRICE_COLUMNS.values():
            result[column] = reference["ingredient_id"].map(countries[column])
        evidence = _evidence_columns(reference)
        for column in evidence:
            result[column] = evidence[column]
        result["n_variants"] = (
            reference["n_variants"] if "n_variants" in reference else 1
        )
        result["aggregation_method"] = (
            reference["aggregation_method"]
            if "aggregation_method" in reference
            else "selected_detail_price"
        )
        return result

    bases = level_frame(base_reference, base_lookup, "base")
    details = level_frame(detail_reference, detail_lookup, "detail")
    bases["parent_base_product_id"] = pd.NA
    details["parent_base_product_id"] = details["canonical_name"].map(
        base_ids.map(lambda value: f"base__{value}")
    )
    if details["parent_base_product_id"].isna().any():
        missing = details.loc[
            details["parent_base_product_id"].isna(), "canonical_name"
        ].unique()
        raise ValueError(f"Detailed products lack base mappings: {sorted(missing)}")

    bases["global_cost_percentile"] = empirical_base_percentiles(
        bases["eu_reference_price_eur_kg"]
    )
    details["global_cost_percentile"] = percentiles_against_base_reference(
        details["eu_reference_price_eur_kg"],
        bases["eu_reference_price_eur_kg"],
    )
    for frame in (bases, details):
        frame["global_cost_tier"] = assign_global_cost_tier(
            frame["eu_reference_price_eur_kg"], thresholds
        )
        boundary = compute_tier_boundary_margin(
            frame["eu_reference_price_eur_kg"], thresholds
        )
        for column in boundary:
            frame[column] = boundary[column]
        frame["cost_reference_version"] = config.cost_reference_version
        for country, price_column in COUNTRY_PRICE_COLUMNS.items():
            frame[f"price_index_{country.lower()}"] = (
                frame[price_column] / frame["eu_reference_price_eur_kg"]
            )

    category_columns = [
        "within_category_rank",
        "within_category_percentile",
        "within_category_position",
        "within_category_resolution",
        "category_size",
    ]
    for column in category_columns:
        bases[column] = bases["source_ingredient_id"].map(category[column])
    details["within_category_rank"] = np.nan
    details["within_category_percentile"] = np.nan
    details["within_category_position"] = pd.NA
    details["within_category_resolution"] = pd.NA
    details["category_size"] = details["canonical_name"].map(
        category.reset_index().set_index("ingredient_name")["category_size"]
    )
    parent_category = category.reset_index().set_index("ingredient_name")
    for column in (
        "within_category_percentile",
        "within_category_position",
        "within_category_resolution",
    ):
        details[f"parent_{column}"] = details["canonical_name"].map(
            parent_category[column]
        )
        bases[f"parent_{column}"] = pd.NA

    details["detail_to_base_price_ratio"] = (
        details["eu_reference_price_eur_kg"]
        / details["canonical_name"].map(base_prices)
    )
    details["detail_vs_base"] = details["detail_to_base_price_ratio"].map(
        lambda ratio: describe_detail_to_base(ratio, config)
    )
    bases["detail_to_base_price_ratio"] = np.nan
    bases["detail_vs_base"] = pd.NA

    nullable_numeric = (
        "within_category_rank",
        "within_category_percentile",
        "parent_within_category_percentile",
        "detail_to_base_price_ratio",
    )
    nullable_text = (
        "parent_base_product_id",
        "within_category_position",
        "within_category_resolution",
        "parent_within_category_position",
        "parent_within_category_resolution",
        "detail_vs_base",
    )
    for frame in (bases, details):
        for column in nullable_numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        for column in nullable_text:
            frame[column] = frame[column].astype("object")
        frame["category_size"] = pd.to_numeric(
            frame["category_size"], errors="coerce"
        ).astype("Int64")

    result = pd.concat([bases, details], ignore_index=True)
    level_order = pd.Categorical(
        result["product_level"], categories=["base", "detail"], ordered=True
    )
    result = (
        result.assign(_level_order=level_order)
        .sort_values(["food_category", "canonical_name", "_level_order", "product_detail"])
        .drop(columns="_level_order")
        .reset_index(drop=True)
    )
    tier_counts = (
        bases["global_cost_tier"].value_counts().reindex(GLOBAL_TIERS, fill_value=0)
    )
    source_datasets = sorted(
        {
            item
            for encoded in base_reference["source_datasets"]
            for item in json.loads(encoded)
        }
    )
    metadata = {
        **asdict(config),
        "number_of_base_products_used": int(
            _valid_prices(base_reference[EU_PRICE_COLUMN]).count()
        ),
        "number_of_detail_products_classified": int(len(detail_reference)),
        "global_threshold_eur_kg_low_mid": thresholds[0],
        "global_threshold_eur_kg_mid_high": thresholds[1],
        "underlying_reference_scope": "EU base products",
        "calibration_input_file": "data/cost/processed/ingredient_price_base_reference.csv",
        "source_price_datasets": source_datasets,
        "base_tier_counts": {tier: int(tier_counts[tier]) for tier in GLOBAL_TIERS},
        "euro_symbol_interpretation": (
            "Relative economic cost tiers; symbols are not literal €1/€2/€3 prices."
        ),
    }
    return result, metadata


def export_enriched_cost_dataset(
    enriched: pd.DataFrame,
    metadata: dict[str, Any],
    output_dir: Path = PROCESSED_DIR,
) -> None:
    """Write canonical classified data and its frozen calibration metadata."""

    write_table(enriched, output_dir / "ingredient_prices_classified.csv")
    metadata_path = output_dir / "cost_classification_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
