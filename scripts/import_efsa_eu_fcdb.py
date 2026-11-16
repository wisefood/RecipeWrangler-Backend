#!/usr/bin/env python3
"""Import complete, non-duplicate EFSA EU-FCDB foods into the EU table."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import _env, get_engine  # noqa: E402


DATA = ROOT / "data/EFSA_EU_FCDB_2026"
FOODS = DATA / "foods_and_foodex2.xlsx"
COMPONENTS = DATA / "component_dictionary.csv"
VALUES = DATA / "composition_values.csv"
TABLE = _env("NUTRITION_EU_TABLE", "nutrients-ingredients-eu")
SOURCE_URL = "https://www.efsa.europa.eu/en/microstrategy/food-composition-data-2026"

# Existing wholesale sources are enriched separately, never re-imported.
SOURCES = {
    "Czech Food Composition Database 2025, Version 10.25": ("efsa_czech", "CZ"),
    "Finnish Food Composition database Fineli 2025": ("fineli", "FI"),
    "FRIDA, Version 5.4, 6/2025": ("frida", "DK"),
    "Greek  Food Composition Database, 2026": ("efsa_greece", "GR"),
    "Icelandic Food Composition Database version 5.0 2025": ("efsa_iceland", "IS"),
    "Italian Food Composition Database CREA AN updated 2024": ("efsa_italy", "IT"),
    "Norwegian Food Composition Database 2026": ("matvaretabellen", "NO"),
    "Portuguese Food Composition Table, 2025": ("efsa_portugal", "PT"),
    "Serbian Food Composition Database 2025": ("efsa_serbia", "RS"),
    "Slovak Food Composition Dataset, 2025": ("efsa_slovakia", "SK"),
    "Slovenian Food Composition Database 2025": ("efsa_slovenia", "SI"),
    "Spanish Food Composition Database. BEDCA v.2.1/2025": ("efsa_spain", "ES"),
    "Swiss Food Composition Database 2025": ("efsa_switzerland", "CH"),
    "The National Food Agency food database, version 2025_10_29": ("slv", "SE"),
}

COMPONENT_KEYS = {
    "protein, total": "Protein",
    "carbohydrate": "Carbohydrate, by difference",
    "fat, total": "Total lipid (fat)",
    "sugars, total": "Sugars, total",
    "fatty acids, total saturated": "Fatty acids, total saturated",
    "sodium": "Sodium, Na",
    "salt": "_salt",
    "fibre, total dietary": "Fiber, total dietary",
    "energy, total metabolisable": "_energy",
}
REQUIRED = {
    "Energy",
    "Energy, kcal",
    "Protein",
    "Carbohydrate, by difference",
    "Total lipid (fat)",
    "Sugars, total",
    "Fatty acids, total saturated",
    "Sodium, Na",
    "Fiber, total dietary",
}
QUALITY_RANK = {"A": 3, "B": 2, "C": 1}


def _normalise_value(component: str, value: str, unit: str) -> tuple[str, float, str] | None:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None
    if component == "_energy":
        if unit == "Kilocalories/100 gram":
            return "Energy, kcal", number, "kcal"
        if unit == "Kilojoule/100 gram":
            return "Energy", number, "kJ"
        return None
    if component == "Sodium, Na":
        if unit == "Gram/100 gram":
            return component, number * 1000, "mg"
        if unit == "Milligram/100 gram":
            return component, number, "mg"
        return None
    if component == "_salt":
        factors = {"Gram/100 gram": 400.0, "Milligram/100 gram": 0.4, "Microgram/100 gram": 0.0004}
        return (component, number * factors[unit], "mg") if unit in factors else None
    if unit == "Gram/100 gram":
        return component, number, "g"
    if unit == "Milligram/100 gram":
        return component, number / 1000, "g"
    return None


def _load_foods() -> dict[tuple[str, str], dict[str, Any]]:
    sheet = load_workbook(FOODS, read_only=True, data_only=True).active
    rows = sheet.iter_rows(min_row=3, values_only=True)
    headers = next(rows)
    index = {name: position for position, name in enumerate(headers)}
    foods: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        dataset = row[index["Dataset name"]]
        if dataset not in SOURCES or row[index["Food Type"]] != "Aggregated Food":
            continue
        code = str(row[index["Original food code"]]).strip()
        source, country = SOURCES[dataset]
        foods[(dataset, code)] = {
            "id": f"{source}:{code}",
            "food_name": str(row[index["English food name"]]).strip(),
            "source": source,
            "country": country,
            "food_group": None if row[index["English food group name"]] == "N_A" else row[index["English food group name"]],
            "foodex2_code": row[index["FoodEx2 code"]],
            "foodex2_description": row[index["FoodEx2 description"]],
            "nutrients": {},
            "_quality": {},
        }
    return foods


def build_records() -> tuple[list[dict[str, Any]], Counter[str]]:
    foods = _load_foods()
    with COMPONENTS.open(encoding="utf-16", newline="") as stream:
        component_names = {
            (row["Dataset name"], row["Original component code"]): row["English component name"].strip().lower()
            for row in csv.DictReader(stream)
        }

    with VALUES.open(encoding="utf-16", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["Dataset name"], row["Original food code"])
            food = foods.get(key)
            if food is None or row["Matrix Unit"] != "Edible portion":
                continue
            component = COMPONENT_KEYS.get(component_names.get((key[0], row["Original component code"]), ""))
            if component is None:
                continue
            normalised = _normalise_value(component, row["Value"], row["Value Unit"])
            if normalised is None:
                continue
            output_key, value, unit = normalised
            rank = QUALITY_RANK.get(row["Quality assessment value"], 0)
            if rank < food["_quality"].get(output_key, -1):
                continue
            food["_quality"][output_key] = rank
            food["nutrients"][output_key] = {
                "unit": unit,
                "value": value,
                "source_code": f"EFSA:{row['Original component code']}:{row['Value id']}",
                "quality": row["Quality assessment value"],
                "value_type": row["Value type"],
                "reference": row["Value reference"],
            }

    rejected: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    for food in foods.values():
        nutrients = food["nutrients"]
        if "Energy" not in nutrients and "Energy, kcal" in nutrients:
            entry = nutrients["Energy, kcal"]
            nutrients["Energy"] = {**entry, "unit": "kJ", "value": entry["value"] * 4.184, "source_code": entry["source_code"] + ":kcal_to_kJ"}
        if "Energy, kcal" not in nutrients and "Energy" in nutrients:
            entry = nutrients["Energy"]
            nutrients["Energy, kcal"] = {**entry, "unit": "kcal", "value": entry["value"] / 4.184, "source_code": entry["source_code"] + ":kJ_to_kcal"}
        if "Sodium, Na" not in nutrients and "_salt" in nutrients:
            entry = nutrients["_salt"]
            nutrients["Sodium, Na"] = {**entry, "unit": "mg", "source_code": entry["source_code"] + ":salt_to_sodium"}
        nutrients.pop("_salt", None)
        missing = REQUIRED - nutrients.keys()
        if missing:
            rejected[",".join(sorted(missing))] += 1
            continue
        food.pop("_quality")
        food["source_url"] = SOURCE_URL
        records.append(food)
    if any(REQUIRED - record["nutrients"].keys() for record in records):
        raise AssertionError("Incomplete EFSA record passed the import safety gate")
    return records, rejected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Insert records; default is dry-run")
    args = parser.parse_args()
    records, rejected = build_records()
    counts = Counter(record["source"] for record in records)
    print(f"eligible complete foods: {len(records)}")
    for source, count in sorted(counts.items()):
        print(f"  {source}: {count}")
    print(f"incomplete aggregated foods excluded: {sum(rejected.values())}")
    if not args.apply:
        print("[dry-run] no database changes")
        return 0

    statement = text(f'''INSERT INTO "{TABLE}" (id, food_name, source, country, food_group, nutrients, source_url)
        VALUES (:id, :food_name, :source, :country, :food_group, CAST(:nutrients AS jsonb), :source_url)
        ON CONFLICT (id) DO NOTHING''')
    inserted = 0
    with get_engine().begin() as connection:
        for start in range(0, len(records), 500):
            params = [{**row, "nutrients": json.dumps(row["nutrients"])} for row in records[start:start + 500]]
            inserted += connection.execute(statement, params).rowcount
    print(f"inserted: {inserted}; preserved existing IDs: {len(records) - inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
