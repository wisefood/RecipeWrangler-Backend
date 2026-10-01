"""Normalize heterogeneous EC Agri-food price workbooks into one table."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .constants import (
    COUNTRY_NAME_TO_CODE,
    EC_MEMBER_STATE_IDS,
    PROCESSED_DIR,
    SOURCE_DIR,
)
from .io import write_table
from .normalize_products import ProductMapping, map_product


NORMALIZED_COLUMNS = [
    "source_dataset", "source_file", "source_sheet", "source_country_code",
    "source_country", "product_raw", "product_normalized", "product_detail",
    "food_group", "price_type_raw", "market_stage", "date", "year", "month",
    "price_original", "unit_original", "currency", "eur_per_kg", "pli_category",
    "notes",
]

DATASETS = {
    "beef-cuts.xlsx": "beef_cuts",
    "beef-live-animals-FpCWS.xlsx": "beef_live_animals",
    "cereals.xlsx": "cereals",
    "dairy.xlsx": "dairy",
    "eggs.xlsx": "eggs",
    "fertiliser.xlsx": "fertiliser",
    "fruits-vegetables.xlsx": "fruit_vegetables",
    "oilseeds.xlsx": "oilseeds",
    "olive-oil.xlsx": "olive_oil",
    "pigmeat-cuts.xlsx": "pigmeat_cuts",
    "pigmeat-piglets.xlsx": "pigmeat_piglets",
    "poultry.xlsx": "poultry",
    "protein-crops.xlsx": "protein_crops",
    "raw-milk.xlsx": "raw_milk",
    "rice.xlsx": "rice",
    "sheep-goat-meat.xlsx": "sheep_goat_meat",
    "sugar.xlsx": "sugar",
    "wine.xlsx": "wine",
}

UNITS = {
    "beef_cuts": ("Price (€/100kg)", "€/100kg", 0.01),
    "cereals": ("Price (€/Tonne)", "€/tonne", 0.001),
    "dairy": ("Price (€/100kg)", "€/100kg", 0.01),
    "eggs": ("Price (€/100kg)", "€/100kg", 0.01),
    "fruit_vegetables": ("Price in EUR / 100kg", "€/100kg", 0.01),
    "oilseeds": ("Price (EUR)", "Unit of Measure", None),
    "olive_oil": ("Price (€/100kg)", "€/100kg", 0.01),
    "pigmeat_cuts": ("Price (€/100kg)", "€/100kg", 0.01),
    "poultry": ("Price (€/100kg)", "€/100kg", 0.01),
    "protein_crops": ("Price (EUR)", "unit missing", None),
    "raw_milk": ("Price(€/100kg)", "€/100kg", 0.01),
    "rice": ("Price (€/Tonne)", "€/tonne", 0.001),
    "sheep_goat_meat": ("Price (€/100kg)", "€/100kg", 0.01),
    "sugar": ("Price (€/tonne)", "€/tonne", 0.001),
}


def normalize_country(value: Any) -> tuple[str, str]:
    if isinstance(value, (int, np.integer)) or (
        isinstance(value, float) and value.is_integer()
    ):
        key = int(value)
        if key not in EC_MEMBER_STATE_IDS:
            raise ValueError(f"unknown_member_state_id:{key}")
        return EC_MEMBER_STATE_IDS[key]
    value = str(value).strip()
    code = COUNTRY_NAME_TO_CODE.get(value)
    if code is None:
        raise ValueError(f"non_member_state:{value}")
    return code, "European Union" if code == "EU27" else value


def normalize_stage(raw: Any, dataset: str) -> str:
    value = "" if raw is None else str(raw).strip()
    lowered = value.lower()
    direct = {
        "retail selling price": "retail_selling",
        "retail buying price": "retail_buying",
        "retail buying prices": "retail_buying",
        "non-retail buying price": "non_retail_buying",
        "non-retail buying prices": "non_retail_buying",
        "selling price": "selling",
        "ex-packaging station price": "ex_packaging",
        "farmgate price": "farm_gate",
        "price at farm gate": "farm_gate",
    }
    if lowered in direct:
        return direct[lowered]
    tokens = {
        "fgate": "farm_gate",
        "delfirst": "delivered_first_customer",
        "delproc": "delivered_processor",
        "depproc": "departure_processor",
        "depprod": "departure_production",
        "depsilo": "departure_silo",
        "delport": "delivered_port",
        "fob": "fob",
        "cif": "cif",
    }
    for token, normalized in tokens.items():
        if re.search(rf"\b{token}\b", lowered):
            return normalized
    phrase_tokens = {
        "departure from farm": "departure_production",
        "departure from silo": "departure_silo",
        "delivered to processor": "delivered_processor",
        "deliver to first customer": "delivered_first_customer",
        "delivered to a port": "delivered_port",
        "delivered to port": "delivered_port",
        "free on board": "fob",
        "cost, insurance and freight": "cif",
    }
    for phrase, normalized in phrase_tokens.items():
        if phrase in lowered:
            return normalized
    defaults = {
        "raw_milk": "producer_price",
        "dairy": "unspecified_market_price",
        "eggs": "unspecified_market_price",
        "olive_oil": "unspecified_market_price",
        "rice": "unspecified_market_price",
        "sheep_goat_meat": "unspecified_market_price",
        "sugar": "selling",
    }
    if dataset in defaults:
        return defaults[dataset]
    return "unspecified" if lowered in {"", "not defined", "nan"} else "other_commodity"


def _source_date(row: pd.Series, dataset: str) -> pd.Timestamp:
    candidates = {
        "cereals": ["Reference period"],
        "eggs": ["Reference Period"],
        "olive_oil": ["Reference Period"],
        "pigmeat_piglets": ["Reference Period"],
        "rice": ["Reference Period"],
        "wine": ["Start date"],
        "oilseeds": ["Week Begin Date"],
    }.get(dataset, ["Begin Date"])
    for column in candidates:
        if column in row and pd.notna(row[column]):
            return pd.Timestamp(row[column])
    if dataset == "sugar":
        return pd.to_datetime(str(row["Year-Month"]), format="%Y/%m")
    if dataset == "fertiliser":
        return pd.to_datetime(f"{int(row['Year'])}-{row['Month']}-01")
    raise ValueError("missing_date")


def _price_type(row: pd.Series, dataset: str) -> str:
    for column in ("Price Type", "Product Stage", "Stage Name", "Market Stage", "Market/Stage", "Contract type"):
        if column in row:
            return str(row[column]).strip()
    return ""


def _unit_and_value(row: pd.Series, dataset: str) -> tuple[float, str, float]:
    column, unit_spec, multiplier = UNITS[dataset]
    value = float(row[column])
    unit = str(row[unit_spec]).strip() if unit_spec in row.index else unit_spec
    unit_key = unit.lower().replace(" ", "")
    if multiplier is None:
        if unit_key in {"€/tonne", "€/ton", "nationalcurrency/ton"}:
            multiplier = 0.001
        elif unit_key in {"€/100kg", "100kg", "nationalcurrency/100kg"}:
            multiplier = 0.01
        elif unit_key in {"€/kg"}:
            multiplier = 1.0
        else:
            raise ValueError(f"incompatible_or_missing_unit:{unit}")
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"invalid_price:{value}")
    return value, unit, value * multiplier


def _row_notes(row: pd.Series, dataset: str) -> str:
    notes = []
    for column in (
        "Marketing Year", "Marketing year", "Week", "Week No.", "WeekNo",
        "Week Number", "Marketing Week", "End Date", "Period Type", "Period",
        "Market", "Market Name", "Sugar region", "Product Type", "Rice Type",
        "Variety", "Farming Method", "Is Calculated", "Is Regulated",
    ):
        if column in row:
            notes.append(f"{column}={row[column]}")
    if dataset in {"dairy", "eggs", "olive_oil", "rice", "sheep_goat_meat"}:
        notes.append("source does not identify a consumer retail stage")
    return "; ".join(notes)


def _precheck(dataset: str, row: pd.Series) -> None:
    if dataset == "fertiliser":
        raise ValueError("non_food_input")
    if dataset == "wine":
        raise ValueError("volume_unit_requires_density")
    if dataset == "beef_live_animals":
        raise ValueError("live_animal_not_recipe_ingredient")
    if dataset == "pigmeat_piglets":
        raise ValueError("portal_reports_piglets_per_head_despite_workbook_header")
    if dataset == "protein_crops":
        raise ValueError("source_unit_missing")
    if dataset == "cereals":
        raw = str(row["Product Name"])
        if "FEED" in raw.upper() or raw.lower().startswith("feed ") or raw == "Triticale":
            raise ValueError("feed_grade_not_recipe_ingredient")
    if dataset == "rice" and str(row["Stage"]).strip().lower() == "paddy":
        raise ValueError("unmilled_paddy_not_recipe_ingredient")


def _stage_raw(row: pd.Series, dataset: str) -> Any:
    for column in ("Price Type", "Product Stage", "Stage Name", "Market Stage", "Market/Stage", "Contract type"):
        if column in row:
            return row[column]
    return None


def normalize_prices(
    source_dir: Path = SOURCE_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    normalized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    mappings: dict[tuple[str, str, str], ProductMapping] = {}

    for filename, dataset in DATASETS.items():
        path = source_dir / filename
        frame = pd.read_excel(path, sheet_name="MyWorkSheet-1")
        for source_row, (_, row) in enumerate(frame.iterrows(), start=2):
            rejection_base = {
                "source_dataset": dataset,
                "source_file": filename,
                "source_sheet": "MyWorkSheet-1",
                "source_row": source_row,
                "source_values": json.dumps(row.to_dict(), default=str, ensure_ascii=False),
            }
            try:
                if dataset in {
                    "beef_cuts", "cereals", "dairy", "eggs", "fruit_vegetables",
                    "oilseeds", "olive_oil", "pigmeat_cuts", "poultry",
                    "protein_crops", "raw_milk", "rice", "sheep_goat_meat", "sugar",
                }:
                    mapping = map_product(dataset, row)
                    mappings[(dataset, mapping.raw_product, mapping.product_detail)] = mapping
                _precheck(dataset, row)
                country_code, country = normalize_country(row.get("Member State", "European Union"))
                date = _source_date(row, dataset)
                original, unit, eur_per_kg = _unit_and_value(row, dataset)
                price_type = _price_type(row, dataset)
                normalized.append(
                    {
                        "source_dataset": dataset,
                        "source_file": filename,
                        "source_sheet": "MyWorkSheet-1",
                        "source_country_code": country_code,
                        "source_country": country,
                        "product_raw": mapping.raw_product,
                        "product_normalized": mapping.canonical_product,
                        "product_detail": mapping.product_detail,
                        "food_group": mapping.food_group,
                        "price_type_raw": price_type,
                        "market_stage": normalize_stage(_stage_raw(row, dataset), dataset),
                        "date": date.date().isoformat(),
                        "year": date.year,
                        "month": date.month,
                        "price_original": original,
                        "unit_original": unit,
                        "currency": "EUR",
                        "eur_per_kg": eur_per_kg,
                        "pli_category": mapping.pli_category,
                        "notes": _row_notes(row, dataset),
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                rejected.append({**rejection_base, "reject_reason": str(exc)})

    normalized_frame = pd.DataFrame(normalized, columns=NORMALIZED_COLUMNS)
    rejected_frame = pd.DataFrame(rejected)
    mapping_frame = pd.DataFrame(
        [
            {"source_dataset": dataset, **mapping.as_dict()}
            for (dataset, _, _), mapping in sorted(mappings.items())
        ]
    ).drop_duplicates()

    assert not normalized_frame.empty
    assert normalized_frame["eur_per_kg"].gt(0).all()
    assert normalized_frame["source_country_code"].notna().all()
    assert normalized_frame["market_stage"].notna().all()
    assert not normalized_frame.duplicated().any(), (
        "Distinct source rows became exact normalized duplicates; preserve the missing source dimension"
    )
    return normalized_frame, rejected_frame, mapping_frame


def main() -> None:
    normalized, rejected, mappings = normalize_prices()
    write_table(normalized, PROCESSED_DIR / "ec_prices_normalized.csv")
    write_table(rejected, PROCESSED_DIR / "ec_prices_rejected.csv", parquet=False)
    write_table(mappings, PROCESSED_DIR / "product_mapping.csv", parquet=False)
    print(f"Normalized {len(normalized)} rows; rejected {len(rejected)} rows")


if __name__ == "__main__":
    main()
