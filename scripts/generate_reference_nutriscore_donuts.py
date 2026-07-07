#!/usr/bin/env python3
"""Generate the unified Section 5.4.2 reference Nutri-Score donut figure."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "section5_outputs"
INPUT = OUT / "descriptive_nutriscore_distribution.csv"
OUTPUT = OUT / "fig_phase1_reference_nutriscore_distributions.png"

GRADES = ["A", "B", "C", "D", "E"]
COLORS = ["#6FA66A", "#A8BC72", "#D7C56A", "#D49A5B", "#C9786F"]
SOURCES = [
    ("Irish Curated Recipes", "Irish Curated Recipes reference"),
    ("HealthyFoods", "HealthyFoods reference"),
    ("Recipe1M / HUMMUS profiled overlap", "Recipe1M / HUMMUS reference"),
    ("MyPlate", "MyPlate reference"),
]


def percentage_label(value: float) -> str:
    return f"{value:.1f}%" if value >= 2.0 else ""


def main() -> None:
    frame = pd.read_csv(INPUT)
    reference = frame[
        frame["profile_or_reference"].eq("Reference")
        & frame["source_or_subset"].isin(source for source, _ in SOURCES)
    ].copy()
    if len(reference) != len(SOURCES):
        raise RuntimeError("Expected one reference distribution row for each source")

    figure, axes_grid = plt.subplots(2, 2, figsize=(12.0, 10.0))
    axes = [axes_grid[0, 0], axes_grid[0, 1], axes_grid[1, 0], axes_grid[1, 1]]
    for axis, (source, title) in zip(axes, SOURCES):
        row = reference[reference["source_or_subset"].eq(source)].iloc[0]
        values = [float(row[f"grade_{grade}_pct"]) for grade in GRADES]
        wedges, _, autotexts = axis.pie(
            values,
            colors=COLORS,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 1.5},
            autopct=lambda pct: percentage_label(pct),
            pctdistance=0.80,
        )
        for text in autotexts:
            text.set_fontsize(9)
            text.set_fontweight("bold")
            text.set_color("#222222")
        axis.text(0, 0, f"n = {int(row['n_recipes']):,}", ha="center", va="center",
                  fontsize=11, fontweight="bold", color="#444444")
        axis.set_title(title, fontsize=13, fontweight="bold", pad=14)
        axis.set_aspect("equal")

    figure.legend(wedges, [f"Grade {grade}" for grade in GRADES], loc="lower center",
                  ncol=5, frameon=True, bbox_to_anchor=(0.5, 0.02), fontsize=10)
    figure.suptitle("Reference Nutri-Score grade distributions", fontsize=18,
                    fontweight="bold", y=0.98)
    figure.subplots_adjust(left=0.05, right=0.95, top=0.90, bottom=0.10, wspace=0.10, hspace=0.15)
    figure.savefig(OUTPUT, dpi=600, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    print(OUTPUT.name)


if __name__ == "__main__":
    main()
