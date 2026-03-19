#!/usr/bin/env python3
"""Fetch USDA food matches for ingredient names and write CSV pairs."""

from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List

import requests

BASE_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OUTPUT_COLUMNS = ["Original Name", "USDA Description", "FDC ID", "URL"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input-csv",
        type=Path,
        default=Path("data/processed/recipe1m/scan_unique_missing_usda_id.csv"),
        help="CSV containing ingredient names.",
    )
    p.add_argument(
        "--ingredient-col",
        default="ingredient",
        help="Ingredient column name in input CSV.",
    )
    p.add_argument(
        "--output-csv",
        type=Path,
        default=Path("notebooks/usda_ingredients.csv"),
        help="Output CSV to create/update.",
    )
    p.add_argument(
        "--api-key",
        default=os.getenv("USDA_API_KEY", ""),
        help="USDA API key. If omitted, uses USDA_API_KEY env var.",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=10,
        help="USDA search page size for candidate ranking.",
    )
    p.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.1,
        help="Delay between API calls.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of ingredients to process (0 = all).",
    )
    p.add_argument(
        "--retry-no-match",
        action="store_true",
        help="Re-query rows already present in output that have 'No Match Found'.",
    )
    return p.parse_args()


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _token_set(text: str) -> set[str]:
    return set(_norm(text).replace(",", " ").replace("-", " ").split())


def _pick_best_match(ingredient: str, foods: List[dict]) -> dict | None:
    if not foods:
        return None

    ingr_norm = _norm(ingredient)
    ingr_tokens = _token_set(ingredient)
    best = None
    best_score = float("-inf")

    for food in foods:
        desc = str(food.get("description") or "")
        desc_norm = _norm(desc)
        desc_tokens = _token_set(desc)

        score = 0.0
        if desc_norm == ingr_norm:
            score += 100.0
        if ingr_norm and ingr_norm in desc_norm:
            score += 40.0
        overlap = len(ingr_tokens & desc_tokens)
        if ingr_tokens:
            score += 20.0 * (overlap / len(ingr_tokens))

        if score > best_score:
            best = food
            best_score = score

    return best or foods[0]


def search_usda(ingredient: str, api_key: str, page_size: int = 10) -> Dict[str, str]:
    params = {
        "api_key": api_key,
        "query": ingredient,
        "pageSize": max(1, page_size),
        "dataType": ["Foundation", "SR Legacy"],
    }

    try:
        response = requests.get(BASE_URL, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
        foods = data.get("foods") or []
        best = _pick_best_match(ingredient, foods)
        if best:
            fdc_id = best.get("fdcId")
            description = str(best.get("description") or "")
            if fdc_id:
                return {
                    "Original Name": ingredient,
                    "USDA Description": description,
                    "FDC ID": str(fdc_id),
                    "URL": f"https://fdc.nal.usda.gov/fdc-app.html#/food-details/{fdc_id}/nutrients",
                }
    except Exception as exc:
        return {
            "Original Name": ingredient,
            "USDA Description": f"Error: {exc}",
            "FDC ID": "N/A",
            "URL": "N/A",
        }

    return {
        "Original Name": ingredient,
        "USDA Description": "No Match Found",
        "FDC ID": "N/A",
        "URL": "N/A",
    }


def load_ingredients(path: Path, ingredient_col: str, limit: int = 0) -> List[str]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        values = []
        seen = set()
        for row in reader:
            name = str(row.get(ingredient_col) or "").strip()
            if not name:
                continue
            key = _norm(name)
            if key in seen:
                continue
            seen.add(key)
            values.append(name)
            if limit > 0 and len(values) >= limit:
                break
    return values


def load_existing_output(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        out = {}
        for row in reader:
            name = str(row.get("Original Name") or "").strip()
            if not name:
                continue
            out[_norm(name)] = {
                "Original Name": name,
                "USDA Description": str(row.get("USDA Description") or ""),
                "FDC ID": str(row.get("FDC ID") or ""),
                "URL": str(row.get("URL") or ""),
            }
    return out


def write_output(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Missing API key. Use --api-key or USDA_API_KEY env var.")

    ingredients = load_ingredients(args.input_csv, args.ingredient_col, args.limit)
    existing = load_existing_output(args.output_csv)

    print(f"Loaded ingredients: {len(ingredients)}")
    print(f"Existing output rows: {len(existing)}")

    for idx, ingredient in enumerate(ingredients, start=1):
        key = _norm(ingredient)
        existing_row = existing.get(key)
        if existing_row:
            is_no_match = _norm(existing_row.get("USDA Description", "")) == _norm("No Match Found")
            if not (args.retry_no_match and is_no_match):
                continue

        row = search_usda(ingredient=ingredient, api_key=args.api_key, page_size=args.page_size)
        existing[key] = row
        if idx % 25 == 0:
            print(f"Processed {idx}/{len(ingredients)}")
        time.sleep(max(0.0, args.sleep_seconds))

    rows = [existing[k] for k in sorted(existing.keys())]
    write_output(args.output_csv, rows)
    print(f"Wrote: {args.output_csv}")
    print(f"Final rows: {len(rows)}")


if __name__ == "__main__":
    main()
