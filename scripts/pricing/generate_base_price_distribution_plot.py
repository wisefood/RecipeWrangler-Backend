"""Generate the cost-catalogue and recipe-category distribution figures.

Run with:
    uv run python scripts/pricing/generate_base_price_distribution_plot.py

The script deliberately produces three separate figures:

* detailed source-derived product forms (for example, a particular fish form),
* canonical base products used for general fallbacks (for example, chicken), and
* the public EU recipe-cost category distribution.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data/cost/processed/ingredient_prices_classified.parquet"
METADATA = ROOT / "data/cost/processed/cost_classification_metadata.json"
RECIPE_FACETS = (
    ROOT / "data/cost/processed/recipe_cost_categories/eu_recipe_cost_facets.csv"
)
FIGURES_DIR = ROOT / "docs/figures"
DETAIL_OUTPUT = FIGURES_DIR / "cost_detailed_product_price_distribution_eu.png"
BASE_OUTPUT = FIGURES_DIR / "cost_base_product_price_distribution_eu.png"
RECIPE_OUTPUT = FIGURES_DIR / "recipe_cost_category_distribution_eu.png"

TIER_ORDER = ("€", "€€", "€€€")
TIER_COLOURS = {"€": "#2E8B57", "€€": "#E69F00", "€€€": "#C44536"}


def _metadata() -> tuple[float, float]:
    metadata = pd.read_json(METADATA, typ="series")
    return (
        float(metadata["global_threshold_eur_kg_low_mid"]),
        float(metadata["global_threshold_eur_kg_mid_high"]),
    )


def _price_frame(prices: pd.DataFrame, level: str) -> pd.DataFrame:
    columns = [
        "canonical_name",
        "product_detail",
        "eu_reference_price_eur_kg",
        "global_cost_tier",
    ]
    result = prices.loc[prices["product_level"].eq(level), columns].dropna(
        subset=["eu_reference_price_eur_kg", "global_cost_tier"]
    )
    result = result.rename(columns={"eu_reference_price_eur_kg": "price"}).copy()
    result["label"] = result["canonical_name"]
    if level == "detail":
        detail = result["product_detail"].fillna("").str.strip()
        result.loc[detail.ne(""), "label"] = (
            result.loc[detail.ne(""), "canonical_name"] + " — " + detail[detail.ne("")]
        )
    return result


def _plot_price_distribution(
    products: pd.DataFrame,
    *,
    title: str,
    product_label: str,
    output: Path,
    low_mid: float,
    mid_high: float,
) -> None:
    """Plot one catalogue level against the fixed EU base-product thresholds."""

    fig, (histogram, strip) = plt.subplots(
        2,
        1,
        figsize=(12, 8),
        sharex=True,
        height_ratios=(3, 1.45),
        layout="constrained",
    )
    fig.suptitle(
        f"{title} (n={len(products)})", fontsize=16, fontweight="bold"
    )

    bins = np.geomspace(
        products["price"].min() * 0.9, products["price"].max() * 1.1, 26
    )
    for tier in TIER_ORDER:
        values = products.loc[products["global_cost_tier"].eq(tier), "price"]
        histogram.hist(
            values,
            bins=bins,
            alpha=0.76,
            color=TIER_COLOURS[tier],
            label=f"{tier}: {len(values)} products",
        )

    for threshold, label in ((low_mid, "€ / €€ boundary"), (mid_high, "€€ / €€€ boundary")):
        histogram.axvline(threshold, color="#303030", linestyle="--", linewidth=1.25)
        histogram.text(
            threshold,
            histogram.get_ylim()[1] * 0.08,
            f"{label}\n€{threshold:.2f}/kg",
            ha="right",
            va="bottom",
            fontsize=9,
            rotation=90,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.83, "pad": 2},
        )
    histogram.set_xscale("log")
    histogram.set_ylabel(f"Number of {product_label}")
    histogram.legend(frameon=False, loc="upper left")
    histogram.grid(axis="y", alpha=0.2)

    positions = {"€": 0, "€€": 1, "€€€": 2}
    rng = np.random.default_rng(20260902)
    for tier in TIER_ORDER:
        values = products.loc[products["global_cost_tier"].eq(tier), "price"]
        y = positions[tier] + rng.uniform(-0.18, 0.18, len(values))
        strip.scatter(
            values,
            y,
            color=TIER_COLOURS[tier],
            alpha=0.8,
            s=40,
            edgecolors="white",
            linewidths=0.4,
        )
    for threshold in (low_mid, mid_high):
        strip.axvline(threshold, color="#303030", linestyle="--", linewidth=1.25)
    strip.set_xscale("log")
    strip.set_yticks(list(positions.values()), list(positions))
    strip.set_xlabel("EU reference price (EUR/kg, logarithmic scale)")
    strip.set_ylabel("Cost class")
    strip.grid(axis="x", alpha=0.2)

    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {output.relative_to(ROOT)}")


def _plot_recipe_distribution(facets: pd.DataFrame, output: Path) -> None:
    """Show the public EU low/medium/high result, including unavailable rows."""

    order = ["low", "medium", "high", "unavailable"]
    labels = {"low": "Low (1)", "medium": "Medium (2)", "high": "High (3)", "unavailable": "Unavailable"}
    colours = {
        "low": TIER_COLOURS["€"],
        "medium": TIER_COLOURS["€€"],
        "high": TIER_COLOURS["€€€"],
        "unavailable": "#8A8A8A",
    }
    category = facets["category"].fillna("unavailable").str.lower()
    counts = category.value_counts().reindex(order, fill_value=0)
    total = int(counts.sum())

    fig, (bars, donut) = plt.subplots(1, 2, figsize=(12, 5.8), layout="constrained")
    fig.suptitle("EU recipe cost-category distribution", fontsize=16, fontweight="bold")
    fig.text(
        0.5,
        0.92,
        f"{total:,} recipes; categories are relative to the WiseFood EU reference distribution",
        ha="center",
        fontsize=10,
        color="#4A4A4A",
    )

    positions = np.arange(len(order))
    chart = bars.bar(
        positions,
        counts.values,
        color=[colours[key] for key in order],
        width=0.66,
    )
    bars.set_xticks(positions, [labels[key] for key in order])
    bars.set_ylabel("Recipes")
    bars.grid(axis="y", alpha=0.2)
    bars.set_axisbelow(True)
    for bar, count in zip(chart, counts.values):
        bars.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + total * 0.008,
            f"{int(count):,}\n{count / total:.1%}",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )

    wedges, _ = donut.pie(
        counts.values,
        startangle=90,
        counterclock=False,
        colors=[colours[key] for key in order],
        wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 2},
    )
    donut.text(0, 0.08, f"{total:,}", ha="center", va="center", fontsize=22, fontweight="bold")
    donut.text(0, -0.14, "recipes", ha="center", va="center", fontsize=10, color="#4A4A4A")
    donut.legend(
        wedges,
        [f"{labels[key]} — {counts[key]:,}" for key in order],
        frameon=False,
        loc="center left",
        bbox_to_anchor=(0.98, 0.5),
    )

    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {output.relative_to(ROOT)}")


def main() -> None:
    prices = pd.read_parquet(INPUT)
    low_mid, mid_high = _metadata()
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    _plot_price_distribution(
        _price_frame(prices, "detail"),
        title="EU reference-price distribution for detailed product forms",
        product_label="detailed products",
        output=DETAIL_OUTPUT,
        low_mid=low_mid,
        mid_high=mid_high,
    )
    _plot_price_distribution(
        _price_frame(prices, "base"),
        title="EU reference-price distribution for canonical base products",
        product_label="base products",
        output=BASE_OUTPUT,
        low_mid=low_mid,
        mid_high=mid_high,
    )
    _plot_recipe_distribution(pd.read_csv(RECIPE_FACETS), RECIPE_OUTPUT)


if __name__ == "__main__":
    main()
