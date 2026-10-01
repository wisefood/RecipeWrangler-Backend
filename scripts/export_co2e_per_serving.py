"""Flat export of per-recipe CO2e/serving (kg) grouped by source, + per-source summary."""

import csv
import os
from pathlib import Path
from statistics import mean, median

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine

SCHEMA = os.getenv("NUTRITION_SCHEMA", "public")
TABLE = os.getenv("NUTRITION_PROFILES_TABLE", "nutrients-recipe-profiles")
REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "artifacts" / "viz"
FLAT = OUT_DIR / "co2e_per_serving_per_recipe.csv"
SUMMARY = OUT_DIR / "co2e_per_serving_summary_per_source.csv"

# Sustainability is region-independent; pick one region so each recipe appears once.
QUERY = f'''
    SELECT recipe_id, source, total_sustainability_per_serving AS co2e_per_serving_kg
    FROM "{SCHEMA}"."{TABLE}"
    WHERE nutrition_source = 'eu'
      AND total_sustainability_per_serving IS NOT NULL
    ORDER BY source, recipe_id
'''

def main() -> None:
    """Write recipe-level and per-source sustainability summaries."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    with get_engine().connect() as conn:
        for rid, source, co2e in conn.execute(text(QUERY)):
            rows.append((str(rid), source, float(co2e)))

    with FLAT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["recipe_id", "source", "co2e_per_serving_kg"])
        writer.writerows(rows)

    by_src = {}
    for _, source, value in rows:
        by_src.setdefault(source, []).append(value)

    with SUMMARY.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "source", "n", "mean_co2e_per_serving_kg",
            "median_co2e_per_serving_kg", "min_kg", "max_kg", "zero_count",
        ])
        for source in sorted(by_src):
            values = by_src[source]
            writer.writerow([
                source, len(values), round(mean(values), 4), round(median(values), 4),
                round(min(values), 4), round(max(values), 4),
                sum(1 for value in values if value == 0.0),
            ])

    print(f"WROTE {FLAT}  ({len(rows)} rows)")
    print(f"WROTE {SUMMARY}")
    print("\nPer-source CO2e/serving (kg):")
    print(f"{'source':18}{'n':>8}{'mean':>10}{'median':>10}{'zeros':>8}")
    for source in sorted(by_src):
        values = by_src[source]
        zeros = sum(1 for value in values if value == 0.0)
        print(
            f"{source:18}{len(values):>8}{mean(values):>10.4f}"
            f"{median(values):>10.4f}{zeros:>8}"
        )


if __name__ == "__main__":
    main()
