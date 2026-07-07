#!/usr/bin/env python3
"""Generate Section 5.4.3 reference-only nutrient median figures."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "section5_outputs"
INPUT = OUT / "descriptive_nutriscore_input_stats.csv"

SOURCES = [
    ("RCSI SafeFood", "rcsi", "RCSI SafeFood"),
    ("HealthyFoods", "healthyfoods", "Healthyfood"),
    (
        "Recipe1M / HUMMUS profiled overlap",
        "recipe1m_hummus",
        "Recipe1M / HUMMUS profiled overlap",
    ),
]

NUTRIENTS = [
    ("sugar_g", "Sugars", "g"),
    ("saturated_fat_g", "Saturated fat", "g"),
    ("fibre_g", "Fibre", "g"),
    ("protein_g", "Protein", "g"),
    ("energy_kcal", "Energy", "kcal"),
    ("sodium_mg", "Sodium", "mg"),
]
GRAM_KEYS = ["sugar_g", "saturated_fat_g", "fibre_g", "protein_g"]
LABELS = {key: label for key, label, _ in NUTRIENTS}
UNITS = {key: unit for key, _, unit in NUTRIENTS}

COLORS = {
    "RCSI SafeFood": "#4472C4",
    "HealthyFoods": "#2CA02C",
    "Healthyfood": "#2CA02C",
    "Recipe1M / HUMMUS profiled overlap": "#D95F1E",
}


def reference_rows() -> pd.DataFrame:
    frame = pd.read_csv(INPUT)
    frame = frame[frame["profile_or_reference"].eq("Reference")].copy()
    frame = frame[frame["source_or_subset"].isin([source for source, _, _ in SOURCES])]
    frame = frame[frame["nutrient"].isin([key for key, _, _ in NUTRIENTS])]
    return frame


def padded_limit(values: pd.Series, minimum: float = 1.0) -> float:
    maximum = float(pd.to_numeric(values, errors="coerce").max())
    return max(minimum, maximum * 1.18)


def style_axis(axis: plt.Axes) -> None:
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.grid(axis="x", linestyle="--", alpha=0.25, zorder=0)
    axis.set_axisbelow(True)


def annotate_bar(axis: plt.Axes, value: float, y: float, unit: str, x_limit: float) -> None:
    if value >= x_limit * 0.72:
        x = value - x_limit * 0.025
        horizontal_alignment = "right"
        color = "white"
    else:
        x = value + x_limit * 0.018
        horizontal_alignment = "left"
        color = "#222222"
    axis.text(
        x,
        y,
        f"{value:,.1f} {unit}",
        va="center",
        ha=horizontal_alignment,
        fontsize=10,
        fontweight="bold",
        color=color,
    )


def plot_single_bars(axis: plt.Axes, value: float, label: str, unit: str,
                     color: str, x_limit: float) -> None:
    axis.barh([0], [value], height=0.42, color=color, zorder=3)
    axis.set_yticks([0], [label])
    axis.set_xlim(0, x_limit)
    annotate_bar(axis, value, 0, unit, x_limit)
    style_axis(axis)


def generate_figures(frame: pd.DataFrame) -> list[str]:
    gram_limit = padded_limit(frame[frame["nutrient"].isin(GRAM_KEYS)]["median_value"])
    energy_limit = padded_limit(frame[frame["nutrient"].eq("energy_kcal")]["median_value"])
    sodium_limit = padded_limit(frame[frame["nutrient"].eq("sodium_mg")]["median_value"])
    filenames: list[str] = []

    for source, token, title in SOURCES:
        subset = frame[frame["source_or_subset"].eq(source)].set_index("nutrient")
        color = COLORS[source]
        figure = plt.figure(figsize=(15.2, 6.0))
        grid = figure.add_gridspec(1, 3, width_ratios=[2.25, 1, 1], wspace=0.42)
        grams_axis = figure.add_subplot(grid[0, 0])
        energy_axis = figure.add_subplot(grid[0, 1])
        sodium_axis = figure.add_subplot(grid[0, 2])

        positions = np.arange(len(GRAM_KEYS))
        for position, nutrient in zip(positions, GRAM_KEYS):
            value = subset.loc[nutrient, "median_value"]
            if pd.isna(value):
                grams_axis.text(
                    gram_limit * 0.02,
                    position,
                    "N/A",
                    va="center",
                    ha="left",
                    fontsize=11,
                    fontweight="bold",
                    color="#666666",
                )
            else:
                number = float(value)
                grams_axis.barh(position, number, height=0.56, color=color, zorder=3)
                annotate_bar(grams_axis, number, position, "g", gram_limit)
        grams_axis.set_yticks(positions, [LABELS[key] for key in GRAM_KEYS])
        grams_axis.invert_yaxis()
        grams_axis.set_xlim(0, gram_limit)
        grams_axis.set_xlabel("Median reference value (g)")
        grams_axis.set_title("A. Gram-based nutrients", weight="bold")
        style_axis(grams_axis)

        energy = float(subset.loc["energy_kcal", "median_value"])
        sodium = float(subset.loc["sodium_mg", "median_value"])
        plot_single_bars(energy_axis, energy, "Energy", "kcal", color, energy_limit)
        plot_single_bars(sodium_axis, sodium, "Sodium", "mg", color, sodium_limit)
        energy_axis.set_xlabel("Median reference value (kcal)")
        sodium_axis.set_xlabel("Median reference value (mg)")
        energy_axis.set_title("B. Energy", weight="bold")
        sodium_axis.set_title("C. Sodium", weight="bold")

        figure.suptitle(
            f"Reference Nutri-Score input nutrient medians: {title}\nPer-serving reference values",
            fontsize=17,
            fontweight="bold",
            y=1.02,
        )
        filename = f"fig_phase1_reference_nutrient_medians_{token}.png"
        figure.savefig(OUT / filename, dpi=600, bbox_inches="tight", facecolor="white")
        plt.close(figure)
        filenames.append(filename)
    return filenames


def write_plot_csv(frame: pd.DataFrame) -> None:
    rows = []
    for source, _, _ in SOURCES:
        subset = frame[frame["source_or_subset"].eq(source)].set_index("nutrient")
        for nutrient, _, unit in NUTRIENTS:
            value = subset.loc[nutrient, "median_value"]
            missing = pd.isna(value)
            note = "Reference median per serving."
            if source.startswith("Recipe1M") and nutrient == "fibre_g":
                note = "N/A: fibre is not available in the HUMMUS reference metadata."
            rows.append(
                {
                    "reference_source_or_subset": source,
                    "nutrient": nutrient,
                    "median_value": None if missing else float(value),
                    "unit": unit,
                    "notes": note,
                }
            )
    pd.DataFrame(rows).to_csv(
        OUT / "reference_nutriscore_input_medians_for_plot.csv", index=False
    )


def write_findings() -> None:
    text = """# Phase 1 Reference Nutrient Plot Findings

These figures describe reference nutrient values only. They do not compare the reference data with RecipeWrangler Irish, Hungarian, or EU global profiles; that comparison belongs to Section 5.5.

### fig_phase1_reference_nutrient_medians_rcsi.png
- **What it shows:** Median RCSI SafeFood expert-computed reference values for the six Nutri-Score input nutrients, reported per serving.
- **Main finding:** The RCSI subset has relatively moderate median nutrient values, including 288.5 kcal energy, 7.95 g sugars, 1.1 g saturated fat, 172 mg sodium, 6.55 g fibre, and 18.5 g protein per serving.
- **Caveat:** The subset contains 46 expert-computed recipes and is therefore smaller and more curated than the other reference collections.

### fig_phase1_reference_nutrient_medians_healthyfoods.png
- **What it shows:** Median HealthyFoods source-provided reference values for the six Nutri-Score input nutrients, reported per serving.
- **Main finding:** HealthyFoods also has relatively moderate medians, although energy, sodium, saturated fat, and protein are somewhat higher than in the RCSI subset.
- **Caveat:** Nutrient-specific sample sizes vary because some reference records have individual missing fields.

### fig_phase1_reference_nutrient_medians_recipe1m_hummus.png
- **What it shows:** Median HUMMUS reference nutrient metadata for the 31,447-recipe profiled Recipe1M overlap, reported per serving.
- **Main finding:** Recipe1M / HUMMUS has substantially higher median energy, sugars, saturated fat, sodium, and protein values than RCSI SafeFood and HealthyFoods, reflecting the broader and more heterogeneous web-recipe corpus.
- **Caveat:** Fibre is unavailable in the HUMMUS reference metadata and is shown as N/A. It should not be interpreted as a Recipe1M / HUMMUS reproducibility target.
"""
    (OUT / "phase1_reference_nutrient_plot_findings.md").write_text(text, encoding="utf-8")


def main() -> None:
    frame = reference_rows()
    write_plot_csv(frame)
    filenames = generate_figures(frame)
    write_findings()
    print("created:")
    for filename in filenames:
        print(f"  {filename}")
    print("  reference_nutriscore_input_medians_for_plot.csv")
    print("  phase1_reference_nutrient_plot_findings.md")


if __name__ == "__main__":
    main()
