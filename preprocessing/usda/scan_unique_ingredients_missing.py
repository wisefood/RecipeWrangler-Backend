#!/usr/bin/env python3
"""Scan unique ingredients through ingredient_weight_tool_usda and report missing coverage."""

import argparse
import csv
from pathlib import Path
from typing import Dict, List

from tqdm import tqdm

from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda


DEFAULT_INPUT = Path("data/processed/recipe1m/recipe1m-unmatched-ingredient-weights-llm.csv")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input CSV with ingredient + measurement.")
    p.add_argument("--ingredient-col", default="ingredient", help="Ingredient column name.")
    p.add_argument("--measurement-col", default="sample_measurement", help="Measurement column name.")
    p.add_argument("--batch-size", type=int, default=128, help="Batch size for tool invocation.")
    p.add_argument("--limit", type=int, default=0, help="Optional max unique rows to scan (0 = all).")
    p.add_argument(
        "--missing-out",
        type=Path,
        default=Path("data/processed/recipe1m/scan_unique_missing_rows.csv"),
        help="Output CSV for unresolved rows.",
    )
    return p.parse_args()


def load_unique_rows(
    path: Path,
    ingredient_col: str,
    measurement_col: str,
    limit: int = 0,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ingredient = str(row.get(ingredient_col) or "").strip()
            if not ingredient:
                continue
            key = ingredient.lower()
            if key in seen:
                continue
            seen.add(key)
            measurement = str(row.get(measurement_col) or "").strip()
            rows.append({"ingredient": ingredient, "measurement": measurement})
            if limit > 0 and len(rows) >= limit:
                break
    return rows


def chunks(items: List[Dict[str, str]], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    args = parse_args()
    rows = load_unique_rows(
        path=args.input,
        ingredient_col=args.ingredient_col,
        measurement_col=args.measurement_col,
        limit=args.limit,
    )
    total = len(rows)
    if total == 0:
        print("No rows found.")
        return

    unresolved_rows: List[Dict[str, str]] = []
    resolved = 0
    missing_usda = 0
    other_unresolved = 0

    total_batches = (total + args.batch_size - 1) // args.batch_size
    for batch in tqdm(chunks(rows, args.batch_size), total=total_batches, desc="Scanning batches"):
        names = [r["ingredient"] for r in batch]
        measurements = [r["measurement"] for r in batch]
        result = ingredient_weight_tool_usda.invoke(
            {
                "ingredient_names": names,
                "measurements": measurements,
                "return_details": True,
            }
        )
        details = result.get("details", []) if isinstance(result, dict) else []

        for item, detail in zip(batch, details):
            error = detail.get("error")
            if error is None and detail.get("weight_grams") is not None:
                resolved += 1
                continue

            if error == "missing_usda_id":
                missing_usda += 1
            else:
                other_unresolved += 1

            unresolved_rows.append(
                {
                    "ingredient": item["ingredient"],
                    "measurement": item["measurement"],
                    "error": str(error),
                    "usda_id": str(detail.get("usda_id") or ""),
                    "match_type": str(detail.get("match_type") or ""),
                    "match_source": str(detail.get("usda_match_source") or ""),
                    "match_canonical": str(detail.get("usda_match_canonical") or ""),
                }
            )

    unresolved_total = total - resolved
    print(f"total_unique={total}")
    print(f"resolved={resolved}")
    print(f"unresolved_total={unresolved_total}")
    print(f"missing_usda_id={missing_usda}")
    print(f"other_unresolved={other_unresolved}")
    print(f"resolved_rate_pct={round((resolved / total) * 100.0, 2)}")
    print(f"unresolved_rate_pct={round((unresolved_total / total) * 100.0, 2)}")

    args.missing_out.parent.mkdir(parents=True, exist_ok=True)
    with args.missing_out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ingredient",
                "measurement",
                "error",
                "usda_id",
                "match_type",
                "match_source",
                "match_canonical",
            ],
        )
        writer.writeheader()
        writer.writerows(unresolved_rows)
    print(f"wrote_missing={args.missing_out}")


if __name__ == "__main__":
    main()
