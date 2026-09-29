"""Generate current recipe Nutri-Score, cost, and sustainability charts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import matplotlib.pyplot as plt
import numpy as np
from sqlalchemy import text

from recipe_wrangler.utils.env_loader import load_runtime_env
from recipe_wrangler.utils.nutrition_postgres import get_connection


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "figures" / "recipe_profile_distributions.png"


def _catalog_counts() -> tuple[int, dict[str, int], dict[str, int]]:
    query = {
        "size": 0,
        "aggs": {
            "profiles": {
                "nested": {"path": "profiles"},
                "aggs": {
                    "eu": {
                        "filter": {"term": {"profiles.region": "eu"}},
                        "aggs": {
                            "scores": {
                                "terms": {"field": "profiles.nutri_score", "size": 10}
                            }
                        },
                    }
                },
            },
            "eu_cost": {
                "nested": {"path": "cost"},
                "aggs": {
                    "region": {
                        "filter": {"term": {"cost.region": "EU"}},
                        "aggs": {
                            "categories": {
                                "terms": {"field": "cost.category", "size": 10}
                            }
                        },
                    }
                },
            },
        },
    }
    url = f"{os.getenv('ELASTIC_URL', 'http://localhost:9200').rstrip('/')}/recipes/_search"
    request = Request(
        url,
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)

    total = int(result["hits"]["total"]["value"])
    nutri = {
        bucket["key"]: int(bucket["doc_count"])
        for bucket in result["aggregations"]["profiles"]["eu"]["scores"]["buckets"]
    }
    cost = {
        bucket["key"]: int(bucket["doc_count"])
        for bucket in result["aggregations"]["eu_cost"]["region"]["categories"]["buckets"]
    }
    cost["unclassified"] = total - sum(cost.values())
    return total, nutri, cost


def _sustainability_values() -> np.ndarray:
    statement = text(
        'SELECT total_sustainability_per_serving '
        'FROM "nutrients-recipe-profiles" '
        "WHERE nutrition_source = 'eu' "
        "AND total_sustainability_per_serving IS NOT NULL"
    )
    with get_connection() as connection:
        return np.asarray([float(row[0]) for row in connection.execute(statement)])


def _donut(ax, values, labels, colors, title, total):
    ax.pie(
        values,
        colors=colors,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 2},
    )
    ax.text(0, 0.07, f"{total:,}", ha="center", va="center", fontsize=20, weight="bold")
    ax.text(0, -0.13, "recipes", ha="center", va="center", fontsize=10, color="#667085")
    ax.set_title(title, fontsize=15, weight="bold", pad=14)
    legend = [
        f"{label}: {value:,} ({value / total:.1%})"
        for label, value in zip(labels, values)
    ]
    ax.legend(legend, loc="lower center", bbox_to_anchor=(0.5, -0.24), frameon=False, ncol=2)


def main() -> None:
    load_runtime_env()
    total, nutri, cost = _catalog_counts()
    sustainability = _sustainability_values()
    if sum(nutri.values()) != total or sustainability.size != total:
        raise RuntimeError(
            f"Expected {total:,} complete profiles, got nutri={sum(nutri.values()):,}, "
            f"sustainability={sustainability.size:,}"
        )

    fig, axes = plt.subplots(1, 3, figsize=(18, 6.4), gridspec_kw={"width_ratios": [1, 1, 1.35]})
    fig.suptitle("Recipe profiling overview", fontsize=21, weight="bold", y=0.98)
    fig.text(
        0.5,
        0.925,
        f"Current live catalogue · {total:,} active recipes",
        ha="center",
        fontsize=11,
        color="#667085",
    )

    nutri_labels = list("ABCDE")
    _donut(
        axes[0],
        [nutri.get(label, 0) for label in nutri_labels],
        nutri_labels,
        ["#038141", "#85BB2F", "#FECB02", "#EE8100", "#E63E11"],
        "EU Nutri-Score",
        total,
    )

    cost_labels = ["Low", "Medium", "High", "Unclassified"]
    _donut(
        axes[1],
        [cost.get(label.lower(), 0) for label in cost_labels],
        cost_labels,
        ["#2E8B57", "#F2B134", "#D95D39", "#B8C0CC"],
        "EU recipe cost category",
        total,
    )

    ax = axes[2]
    p99 = float(np.percentile(sustainability, 99))
    median = float(np.median(sustainability))
    mean = float(np.mean(sustainability))
    visible = sustainability[sustainability <= p99]
    ax.hist(visible, bins=32, color="#5F2C77", alpha=0.82, edgecolor="white", linewidth=0.6)
    ax.axvline(median, color="#94B236", linewidth=2.5, label=f"Median: {median:.2f}")
    ax.axvline(mean, color="#D53F60", linewidth=2.5, linestyle="--", label=f"Mean: {mean:.2f}")
    ax.set_title("Carbon footprint per serving", fontsize=15, weight="bold", pad=14)
    ax.set_xlabel("kg CO₂e per serving")
    ax.set_ylabel("Recipes")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#E4E7EC", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False)
    ax.text(
        0.98,
        0.96,
        f"Chart shows 99% of values (≤ {p99:.2f})\nLong-tail maximum: {sustainability.max():.2f} kg CO₂e",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=9,
        color="#667085",
    )

    fig.text(
        0.5,
        0.015,
        "Nutri-Score and sustainability use the calculated EU profile. Cost uses the EU category.",
        ha="center",
        fontsize=9,
        color="#667085",
    )
    fig.subplots_adjust(left=0.045, right=0.98, top=0.84, bottom=0.18, wspace=0.35)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=200, bbox_inches="tight", facecolor="white")
    print(OUTPUT)


if __name__ == "__main__":
    main()
