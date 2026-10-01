"""Validation outputs for the explainable ingredient cost classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .constants import DOCS_DIR, PROCESSED_DIR
from .cost_classification import (
    DEFAULT_COST_CONFIG,
    GLOBAL_TIERS,
    CostClassificationConfig,
    assign_global_cost_tier,
)
from .io import write_table


VALIDATION_DIRNAME = "cost_classification_validation"


def _markdown_table(frame: pd.DataFrame, digits: int = 3) -> str:
    data = frame.copy()
    for column in data.select_dtypes(include="number"):
        data[column] = data[column].map(
            lambda value: "" if pd.isna(value) else f"{value:.{digits}f}"
        )
    data = data.fillna("")
    headers = [str(column) for column in data.columns]
    rows = [[str(value) for value in row] for row in data.itertuples(index=False, name=None)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def catalogue_growth_sensitivity(
    base_products: pd.DataFrame,
    config: CostClassificationConfig = DEFAULT_COST_CONFIG,
    *,
    iterations: int = 1000,
    additional_products: int | None = None,
    random_seed: int = 42,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Estimate tier flips when plausible products are added to the catalogue.

    Added prices are sampled from the observed empirical base-price distribution.
    They are used for analysis only and never assign production labels.
    """

    if iterations <= 0:
        raise ValueError("iterations must be positive")
    prices = pd.to_numeric(
        base_products["eu_reference_price_eur_kg"], errors="coerce"
    )
    if prices.isna().any() or prices.le(0).any():
        raise ValueError("Catalogue sensitivity requires complete positive base prices")
    count = len(prices)
    added = additional_products or max(1, round(count * 0.10))
    original_thresholds = (
        float(np.quantile(prices, 1 / 3, method=config.quantile_method)),
        float(np.quantile(prices, 2 / 3, method=config.quantile_method)),
    )
    original_tiers = assign_global_cost_tier(prices, original_thresholds).to_numpy()
    rng = np.random.default_rng(random_seed)
    flip_counts = np.zeros(count, dtype=int)
    flip_rates: list[float] = []
    threshold_rows: list[tuple[float, float]] = []
    observed = prices.to_numpy(dtype=float)
    for _ in range(iterations):
        simulated = rng.choice(observed, size=added, replace=True)
        expanded = np.concatenate([observed, simulated])
        thresholds = (
            float(np.quantile(expanded, 1 / 3, method=config.quantile_method)),
            float(np.quantile(expanded, 2 / 3, method=config.quantile_method)),
        )
        tiers = assign_global_cost_tier(prices, thresholds).to_numpy()
        flipped = tiers != original_tiers
        flip_counts += flipped
        flip_rates.append(float(flipped.mean()))
        threshold_rows.append(thresholds)
    per_product = base_products[
        [
            "product_id",
            "canonical_name",
            "food_category",
            "eu_reference_price_eur_kg",
            "global_cost_tier",
        ]
    ].copy()
    per_product["tier_flip_probability"] = flip_counts / iterations
    threshold_values = np.asarray(threshold_rows)
    summary = {
        "method": "empirical catalogue-growth resampling",
        "analysis_only": True,
        "iterations": iterations,
        "random_seed": random_seed,
        "base_product_count": count,
        "simulated_additional_products_per_iteration": added,
        "mean_tier_flip_rate": float(np.mean(flip_rates)),
        "p95_tier_flip_rate": float(np.quantile(flip_rates, 0.95)),
        "maximum_tier_flip_rate": float(np.max(flip_rates)),
        "mean_simulated_t1_eur_kg": float(threshold_values[:, 0].mean()),
        "mean_simulated_t2_eur_kg": float(threshold_values[:, 1].mean()),
    }
    return summary, per_product.sort_values(
        "tier_flip_probability", ascending=False
    ).reset_index(drop=True)


def build_validation_tables(
    enriched: pd.DataFrame,
    metadata: dict[str, Any],
) -> dict[str, pd.DataFrame]:
    """Build inspectable distribution, boundary, category, and missing-data tables."""

    bases = enriched[enriched["product_level"] == "base"].copy()
    details = enriched[enriched["product_level"] == "detail"].copy()
    base_sorted = bases[
        [
            "product_id",
            "canonical_name",
            "food_category",
            "eu_reference_price_eur_kg",
            "global_cost_percentile",
            "global_cost_tier",
            "price_evidence_confidence",
        ]
    ].sort_values("eu_reference_price_eur_kg")

    tier_rows: list[dict[str, Any]] = []
    for tier in GLOBAL_TIERS:
        group = bases[bases["global_cost_tier"] == tier]
        tier_rows.append(
            {
                "global_cost_tier": tier,
                "product_count": len(group),
                "minimum_eur_kg": group["eu_reference_price_eur_kg"].min(),
                "median_eur_kg": group["eu_reference_price_eur_kg"].median(),
                "maximum_eur_kg": group["eu_reference_price_eur_kg"].max(),
                "category_composition": json.dumps(
                    group["food_category"].value_counts().sort_index().to_dict(),
                    ensure_ascii=False,
                ),
            }
        )
    tier_summary = pd.DataFrame(tier_rows)

    boundary_rows = []
    for name, threshold in (
        ("T1", metadata["global_threshold_eur_kg_low_mid"]),
        ("T2", metadata["global_threshold_eur_kg_mid_high"]),
    ):
        below = bases[bases["eu_reference_price_eur_kg"] < threshold].nlargest(
            5, "eu_reference_price_eur_kg"
        )
        above = bases[bases["eu_reference_price_eur_kg"] >= threshold].nsmallest(
            5, "eu_reference_price_eur_kg"
        )
        for side, frame in (("below", below), ("above", above)):
            for row in frame.itertuples(index=False):
                boundary_rows.append(
                    {
                        "boundary": name,
                        "side": side,
                        "threshold_eur_kg": threshold,
                        "canonical_name": row.canonical_name,
                        "food_category": row.food_category,
                        "eu_reference_price_eur_kg": row.eu_reference_price_eur_kg,
                        "global_cost_tier": row.global_cost_tier,
                        "absolute_gap_eur_kg": abs(
                            row.eu_reference_price_eur_kg - threshold
                        ),
                        "tier_boundary_margin_pct": row.tier_boundary_margin_pct,
                    }
                )
    boundary_inspection = pd.DataFrame(boundary_rows)

    category_ranking = bases[
        [
            "food_category",
            "category_size",
            "canonical_name",
            "eu_reference_price_eur_kg",
            "within_category_rank",
            "within_category_percentile",
            "within_category_position",
            "within_category_resolution",
        ]
    ].sort_values(["food_category", "within_category_rank"], na_position="last")

    detail_columns = [
        "product_id",
        "canonical_name",
        "product_detail",
        "food_category",
        "eu_reference_price_eur_kg",
        "detail_to_base_price_ratio",
        "detail_vs_base",
        "global_cost_tier",
        "price_evidence_confidence",
    ]
    detail_extremes = pd.concat(
        [
            details.nsmallest(10, "detail_to_base_price_ratio")[detail_columns],
            details.nlargest(10, "detail_to_base_price_ratio")[detail_columns],
        ],
        ignore_index=True,
    ).drop_duplicates("product_id")

    required_fields = [
        "eu_reference_price_eur_kg",
        "price_ie_eur_kg",
        "price_hu_eur_kg",
        "price_si_eur_kg",
        "food_category",
        "price_evidence_confidence",
        "source_market_stage",
        "source_datasets",
        "provenance",
    ]
    missing_rows = [
        {"field": field, "missing_rows": int(enriched[field].isna().sum())}
        for field in required_fields
    ]
    missing_rows.append(
        {
            "field": "parent_base_product_id (details only)",
            "missing_rows": int(
                details["parent_base_product_id"].isna().sum()
            ),
        }
    )
    missing_report = pd.DataFrame(missing_rows)
    return {
        "base_products_sorted": base_sorted.reset_index(drop=True),
        "global_tier_summary": tier_summary,
        "boundary_inspection": boundary_inspection,
        "category_ranking": category_ranking.reset_index(drop=True),
        "detail_base_extremes": detail_extremes,
        "missing_data_report": missing_report,
    }


def write_validation_report(
    enriched: pd.DataFrame,
    metadata: dict[str, Any],
    *,
    output_dir: Path = PROCESSED_DIR,
    report_dir: Path = DOCS_DIR,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Write validation CSVs, deterministic sensitivity results, and Markdown."""

    tables = build_validation_tables(enriched, metadata)
    validation_dir = output_dir / VALIDATION_DIRNAME
    validation_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        write_table(frame, validation_dir / f"{name}.csv", parquet=False)

    bases = enriched[enriched["product_level"] == "base"].copy()
    details = enriched[enriched["product_level"] == "detail"].copy()
    sensitivity, product_sensitivity = catalogue_growth_sensitivity(bases)
    write_table(
        product_sensitivity,
        validation_dir / "catalogue_growth_product_sensitivity.csv",
        parquet=False,
    )
    (validation_dir / "catalogue_growth_summary.json").write_text(
        json.dumps(sensitivity, indent=2), encoding="utf-8"
    )

    prices = bases["eu_reference_price_eur_kg"]
    distribution = pd.DataFrame(
        [
            {
                "count": len(prices),
                "minimum": prices.min(),
                "q1": prices.quantile(0.25),
                "median": prices.median(),
                "q3": prices.quantile(0.75),
                "maximum": prices.max(),
                "mean": prices.mean(),
                "skewness": prices.skew(),
                "T1": metadata["global_threshold_eur_kg_low_mid"],
                "T2": metadata["global_threshold_eur_kg_mid_high"],
            }
        ]
    )
    write_table(distribution, validation_dir / "global_distribution_summary.csv", parquet=False)
    meat = tables["category_ranking"][
        tables["category_ranking"]["food_category"] == "meat"
    ]
    sugar = tables["category_ranking"][
        tables["category_ranking"]["food_category"]
        == "sugar and confectionery"
    ]
    examples = (
        tables["base_products_sorted"]
        .groupby("global_cost_tier", sort=False)
        .head(3)
    )
    country_estimates_detail = int(
        details[["price_ie_eur_kg", "price_hu_eur_kg", "price_si_eur_kg"]]
        .notna()
        .sum()
        .sum()
    )
    country_estimates_base = int(
        bases[["price_ie_eur_kg", "price_hu_eur_kg", "price_si_eur_kg"]]
        .notna()
        .sum()
        .sum()
    )
    text = f"""# Ingredient Cost Classification Validation

## Dataset

The classifier detected **{len(bases)} base products**, **{len(details)} detailed products**, and **{enriched['food_category'].nunique()} food categories**. It retained **{country_estimates_detail} detailed country estimates** and **{country_estimates_base} derived base-product country estimates** across Ireland, Hungary, and Slovenia.

All prices are **economic reference price estimates** in EUR/kg. They are not exact current supermarket prices.

## Methodology

Only the **{metadata['number_of_base_products_used']} EU base-product prices** calibrate the global thresholds. Detailed forms receive labels from the frozen thresholds but have no influence on them, preventing variety-rich families such as apples or fish from dominating calibration.

Global tiers use tertiles because they provide an interpretable, reasonably balanced structural summary. Quantiles use NumPy's explicit `{metadata['quantile_method']}` method. The canonical labels are EU-relative and remain identical across Ireland, Hungary, and Slovenia; country prices are retained separately through price indices.

Within-category ranking is separate from the global tier. Categories with at least 7 products use normal Low/Middle/High resolution; categories with 4–6 use coarse resolution; categories with 2–3 expose only rank and percentile; single-member categories are unavailable. Evidence confidence remains independent from economic tier.

The symbols `€`, `€€`, and `€€€` are relative economic cost tiers. They do **not** mean literal prices of €1, €2, or €3.

## Learned calibration

- Version: `{metadata['cost_reference_version']}`
- Calibration date: `{metadata['calibration_date']}`
- T1 (€ → €€): **€{metadata['global_threshold_eur_kg_low_mid']:.3f}/kg**
- T2 (€€ → €€€): **€{metadata['global_threshold_eur_kg_mid_high']:.3f}/kg**
- Reference scope: `{metadata['underlying_reference_scope']}`

{_markdown_table(distribution)}

## Global tier composition

{_markdown_table(tables['global_tier_summary'])}

Representative examples:

{_markdown_table(examples[['canonical_name', 'food_category', 'eu_reference_price_eur_kg', 'global_cost_percentile', 'global_cost_tier']])}

## All base products

{_markdown_table(tables['base_products_sorted'])}

## Boundary inspection

The following products lie immediately below or above T1 and T2. `tier_boundary_margin_pct` measures proportional distance from the nearest boundary and is not evidence confidence.

{_markdown_table(tables['boundary_inspection'])}

## Within-category ranking

{_markdown_table(tables['category_ranking'])}

### Meat example

{_markdown_table(meat)}

The four-member meat category is intentionally marked `coarse`. Chicken and beef are both Middle, while their percentiles retain the distinction between 0.375 and 0.625.

### Single-member sugar category

{_markdown_table(sugar)}

Sugar has no within-category percentile or band because comparison with itself is not meaningful.

## Detail/base sanity checks

Details are excluded from calibration. The most extreme detail/base ratios are listed for inspection rather than silently removed.

{_markdown_table(tables['detail_base_extremes'])}

## Missing data

{_markdown_table(tables['missing_data_report'], digits=0)}

Rows with missing classification inputs are reported rather than silently filled or dropped.

## Sensitivity

Catalogue-growth analysis added **{sensitivity['simulated_additional_products_per_iteration']}** empirically sampled plausible products in each of **{sensitivity['iterations']}** deterministic simulations. The mean existing-product tier flip rate was **{100 * sensitivity['mean_tier_flip_rate']:.2f}%** and the 95th-percentile rate was **{100 * sensitivity['p95_tier_flip_rate']:.2f}%**. Simulated products are analysis-only and never assign production labels.

A unified observation bootstrap was not run. Agricultural families mix reporting stages and observation structures, while EUMOFA fish inputs are already monthly medians. Treating these heterogeneous records as exchangeable observations would fabricate a common uncertainty distribution.

## Edge cases and limitations

- Market stages include retail, wholesale-adjacent, farm-gate, producer, and other economic proxies.
- The estimates are not exact supermarket receipts.
- Tiers are relative to version `{metadata['cost_reference_version']}` and its current base-product universe.
- High within-category position does not by itself imply global high cost.
- Within-category position does not establish culinary substitutability.
- Evidence confidence and tier-boundary margin remain separate from economic tier.
- Ingredient EUR/kg tiers do not directly represent recipe affordability.
- Recipe-level cost must later be quantity-weighted and divided by servings before any recipe-level thresholds are calibrated.
"""
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "COST_CLASSIFICATION_VALIDATION.md").write_text(
        text, encoding="utf-8"
    )
    return tables, sensitivity
