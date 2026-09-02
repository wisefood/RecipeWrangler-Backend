"""Shared constants for the ingredient price pipeline."""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COST_DIR = REPOSITORY_ROOT / "data" / "cost"
RAW_DIR = COST_DIR / "raw"
SOURCE_DIR = RAW_DIR / "ec_agri_food"
PLI_SOURCE_DIR = RAW_DIR / "eurostat"
FISH_SOURCE_DIR = RAW_DIR / "eumofa" / "monthly"
PROCESSED_DIR = COST_DIR / "processed"
ARTIFACT_DIR = REPOSITORY_ROOT / "artifacts" / "pricing"
DOCS_DIR = REPOSITORY_ROOT / "docs"

WINDOW_START = "2025-08-01"
WINDOW_END = "2026-07-31"
TARGET_COUNTRIES = ("IE", "HU", "SI")

EU_COUNTRIES = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "EL": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
}

COUNTRY_NAME_TO_CODE = {name: code for code, name in EU_COUNTRIES.items()}
COUNTRY_NAME_TO_CODE["European Union"] = "EU27"
COUNTRY_NAME_TO_CODE["European Commission"] = "EU27"

# EC Agri-food portal internal identifiers. These were cross-checked against
# the portal API on the extract date. Both 0 and 1 denote EU aggregates in the
# supplied exports; individual country identifiers are stable across files.
EC_MEMBER_STATE_IDS = {
    0: ("EU27", "European Union"),
    1: ("EU27", "European Union"),
    10: ("BE", "Belgium"),
    20: ("BG", "Bulgaria"),
    30: ("CZ", "Czechia"),
    40: ("DK", "Denmark"),
    50: ("DE", "Germany"),
    60: ("EE", "Estonia"),
    70: ("IE", "Ireland"),
    80: ("EL", "Greece"),
    90: ("ES", "Spain"),
    100: ("FR", "France"),
    105: ("HR", "Croatia"),
    110: ("IT", "Italy"),
    120: ("CY", "Cyprus"),
    130: ("LV", "Latvia"),
    140: ("LT", "Lithuania"),
    150: ("LU", "Luxembourg"),
    160: ("HU", "Hungary"),
    170: ("MT", "Malta"),
    180: ("NL", "Netherlands"),
    190: ("AT", "Austria"),
    200: ("PL", "Poland"),
    210: ("PT", "Portugal"),
    220: ("RO", "Romania"),
    230: ("SI", "Slovenia"),
    240: ("SK", "Slovakia"),
    250: ("FI", "Finland"),
    260: ("SE", "Sweden"),
}

PLI_CATEGORIES = (
    "Food",
    "Cereals and cereal products",
    "Live animals, meat and other parts of slaughtered land animals",
    "Fish and other seafood",
    "Milk, other dairy products and eggs",
    "Oils and fats",
    "Fruits and nuts",
    "Vegetables, tubers, plantains, cooking bananas and pulses",
    "Sugar, confectionery and desserts",
    "Ready-made food and other food products n.e.c.",
)

STAGE_PRIORITY = {
    "retail_selling": 0,
    "retail_buying": 1,
    "selling": 2,
    "ex_packaging": 3,
    "non_retail_buying": 4,
    "unspecified_market_price": 5,
    "producer_price": 6,
    "farm_gate": 7,
    "delivered_first_customer": 8,
    "delivered_processor": 9,
    "departure_processor": 10,
    "departure_production": 11,
    "departure_silo": 12,
    "delivered_port": 13,
    "fob": 14,
    "cif": 15,
    "other_commodity": 16,
    "unspecified": 17,
}
