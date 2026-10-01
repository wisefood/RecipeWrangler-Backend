"""Build the EU food-composition table from Ciqual, CoFID, NEVO, and reviewed supplements.

Mirrors the USDA jsonb shape so the matcher/profiler can swap tables.

Output table: ``nutrients-ingredients-global`` (configurable via
NUTRITION_GLOBAL_TABLE) — columns: id, food_name, source, country, food_group,
nutrients (jsonb keyed by USDA name when mapped, else by EU canonical name).

Decisions locked with user (see chat):
  1) jsonb value = {unit, value, source_code} — no fake USDA ids
  2) drop composite dishes (Ciqual + NEVO only; CoFID skipped — see TODO below)
  3) keep both Energy (kJ) and "Energy, kcal"
  4) CoFID 'Tr' / 'N' / blanks -> omit key (null, like USDA)
  5) Vitamin A IU = retinol_ug/0.3 + beta_carotene_ug/0.6 (USDA pre-2001)
  6) English food names only
  7) IDs prefixed: ciqual:<code> | cofid:<code> | nevo:<code>
  8) New table, do not touch USDA / Irish / Hungarian
  9) No vector embedding here

Reviewed supplements retain their original provider id and URL in every row.
They fill narrow composition gaps without importing another entire national table.

Run:
  PYTHONPATH=src python scripts/build_eu_global_dataset.py [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from openpyxl import load_workbook  # noqa: F401  (used implicitly by pandas)
from sqlalchemy import text

# Ensure src/ on path when run as a script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recipe_wrangler.utils.nutrition_postgres import get_engine, _env  # noqa: E402

DATA = ROOT / "data" / "EU"
MAPPING_CSV = DATA / "eu_nutrient_mapping.csv"
CIQUAL_XLSX = DATA / "Cicual-FR" / "Table Ciqual 2025_ENG_2025_11_03.xlsx"
COFID_XLSX = DATA / "CoFID-UK" / "McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx"
NEVO_CSV = DATA / "NEVO-NL" / "NEVO2025_v9.0_Details.csv"

TABLE = _env("NUTRITION_EU_TABLE", "nutrients-ingredients-eu")

# Composite/dish groups + orphan placeholder to skip.
CIQUAL_DROP_GROUPS = {"starters and dishes", "ice cream and sorbet", "-"}
NEVO_DROP_GROUPS = {"Mixed dishes", "Soups"}


def _supplemental_nutrients(
    *, energy_kj: float, protein_g: float, carbohydrate_g: float,
    fat_g: float, sugars_g: float, saturated_fat_g: float,
    sodium_mg: float, fibre_g: float, source_code: str,
) -> dict[str, dict[str, Any]]:
    values = {
        "Energy": ("kJ", energy_kj),
        "Protein": ("g", protein_g),
        "Carbohydrate, by difference": ("g", carbohydrate_g),
        "Total lipid (fat)": ("g", fat_g),
        "Sugars, total including NLEA": ("g", sugars_g),
        "Fatty acids, total saturated": ("g", saturated_fat_g),
        "Sodium, Na": ("mg", sodium_mg),
        "Fiber, total dietary": ("g", fibre_g),
    }
    return {
        name: {"unit": unit, "value": value, "source_code": source_code}
        for name, (unit, value) in values.items()
    }


# Official per-100-g records, reviewed 2026-09-23.
# Fineli is maintained by Finland's THL; Frida by DTU Food Institute, Denmark;
# SLV by Sweden's National Food Agency; Matvaretabellen by the Norwegian Food
# Safety Authority. Norwegian rows are narrow reviewed European supplements,
# not a wholesale import into the EU composite table.
SUPPLEMENTAL_FOODS = (
    {
        "id": "fineli:29026",
        "food_name": "Vegetable bouillon, dissolved",
        "source": "fineli",
        "country": "FI",
        "food_group": "Prepared stock and bouillon",
        "source_url": "https://fineli.fi/fineli/en/elintarvikkeet/29026",
        "nutrients": _supplemental_nutrients(
            energy_kj=39, protein_g=0.3, carbohydrate_g=0.4, fat_g=0.7,
            sugars_g=0.1, saturated_fat_g=0.3, sodium_mg=543.8,
            fibre_g=0.1, source_code="Fineli food 29026",
        ),
    },
    {
        "id": "frida:534",
        "food_name": "Bouillon, beef, cube, prepared",
        "source": "frida",
        "country": "DK",
        "food_group": "Prepared stock and bouillon",
        "source_url": "https://frida.fooddata.dk/food/534",
        "nutrients": _supplemental_nutrients(
            energy_kj=28, protein_g=1.1, carbohydrate_g=0.3, fat_g=0.1,
            sugars_g=0.22, saturated_fat_g=0.048, sodium_mg=326,
            fibre_g=0, source_code="Frida food 534",
        ),
    },
    {
        "id": "frida:277",
        "food_name": "Bouillon, chicken, prepared",
        "source": "frida",
        "country": "DK",
        "food_group": "Prepared stock and bouillon",
        "source_url": "https://frida.fooddata.dk/food/277",
        "nutrients": _supplemental_nutrients(
            energy_kj=18, protein_g=0.3, carbohydrate_g=0.3, fat_g=0.2,
            sugars_g=0.2, saturated_fat_g=0.054, sodium_mg=313,
            fibre_g=0, source_code="Frida food 277",
        ),
    },
    {
        "id": "frida:1831",
        "food_name": "Mixed berries, frozen",
        "source": "frida",
        "country": "DK",
        "food_group": "Frozen fruits and berries",
        "source_url": "https://frida.fooddata.dk/food/1831?lang=en",
        "nutrients": _supplemental_nutrients(
            energy_kj=221, protein_g=0.90625, carbohydrate_g=12.34375,
            fat_g=0.7, sugars_g=7.6, saturated_fat_g=0.1,
            sodium_mg=0, fibre_g=3.35, source_code="Frida food 1831",
        ),
    },
    {
        "id": "frida:1771",
        "food_name": "Feta, 5% fat (salad cheese)",
        "source": "frida",
        "country": "DK",
        "food_group": "Semi-firm rennet cheese",
        "source_url": "https://frida.fooddata.dk/food/1771?lang=en",
        "nutrients": _supplemental_nutrients(
            energy_kj=738.12, protein_g=21.692, carbohydrate_g=1.638,
            fat_g=9.23, sugars_g=0, saturated_fat_g=5.671,
            sodium_mg=1150, fibre_g=0, source_code="Frida food 1771",
        ),
    },
    {
        "id": "frida:1962",
        "food_name": "Salad dressing, Italian",
        "source": "frida",
        "country": "DK",
        "food_group": "Dressings",
        "source_url": "https://frida.fooddata.dk/food/1962?lang=en",
        "nutrients": _supplemental_nutrients(
            energy_kj=427.885, protein_g=0, carbohydrate_g=10.905,
            fat_g=6.7, sugars_g=10.15, saturated_fat_g=0.288817,
            sodium_mg=935, fibre_g=0.6, source_code="Frida food 1962",
        ),
    },
    {
        "id": "matvaretabellen:10.197",
        "food_name": "Hoisin sauce, home-made",
        "source": "matvaretabellen",
        "country": "NO",
        "food_group": "Savoury sauces",
        "source_url": "https://www.matvaretabellen.no/en/hoisin-sauce-home-made/",
        "nutrients": _supplemental_nutrients(
            energy_kj=1102, protein_g=2.6, carbohydrate_g=21.6,
            fat_g=18.6, sugars_g=20.2, saturated_fat_g=3.0,
            sodium_mg=2716, fibre_g=0.5,
            source_code="Matvaretabellen food 10.197",
        ),
    },
    {
        "id": "slv:2557",
        "food_name": "Taco shells",
        "source": "slv",
        "country": "SE",
        "food_group": "Unleavened bread, corn",
        "source_url": (
            "https://soknaringsinnehall.livsmedelsverket.se/"
            "Home/FoodDetailsMeta/2557"
        ),
        "nutrients": _supplemental_nutrients(
            energy_kj=2013, protein_g=5.5, carbohydrate_g=59.9,
            fat_g=23.5, sugars_g=0.9, saturated_fat_g=2.0,
            sodium_mg=238, fibre_g=4.0,
            source_code="SLV food 2557, database 2026-07-01",
        ),
    },
    {
        "id": "matvaretabellen:06.140",
        "food_name": "Ginger, canned (pickled)",
        "source": "matvaretabellen",
        "country": "NO",
        "food_group": "Pickles",
        "source_url": "https://www.matvaretabellen.no/en/ginger-canned/",
        "nutrients": _supplemental_nutrients(
            energy_kj=130, protein_g=0.6, carbohydrate_g=6.3,
            fat_g=0.2, sugars_g=1.7, saturated_fat_g=0.0,
            sodium_mg=701, fibre_g=0.6,
            source_code="Matvaretabellen food 06.140 (Fineli source 410d)",
        ),
    },
    {
        "id": "slv:4025",
        "food_name": "Wasabi paste",
        "source": "slv",
        "country": "SE",
        "food_group": "Vegetable condiment",
        "source_url": (
            "https://soknaringsinnehall.livsmedelsverket.se/"
            "Home/FoodDetailsMeta/4025"
        ),
        "nutrients": _supplemental_nutrients(
            energy_kj=1170, protein_g=2.2, carbohydrate_g=40.0,
            fat_g=10.9, sugars_g=13.2, saturated_fat_g=1.8,
            sodium_mg=3390, fibre_g=6.1,
            source_code="SLV food 4025, database 2026-07-01",
        ),
    },
    # Concentrated (as-sold, per 100 g) stock cubes, powders and pots, reviewed 2026-09-25.
    # The "prepared" bouillon rows above are dilute broth and must not stand in for a cube.
    # Rows with source "retail" come from manufacturer/retailer labels (EU), not a national
    # database; sodium = salt x 400 mg, energy kJ = kcal x 4.184 where the label gave kcal only.
    {
        "id": "fineli:29009",
        "food_name": "Chicken bouillon cube",
        "source": "fineli",
        "country": "FI",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://fineli.fi/fineli/en/elintarvikkeet/29009",
        "nutrients": _supplemental_nutrients(
            energy_kj=1360, protein_g=9, carbohydrate_g=14, fat_g=26,
            sugars_g=3.8, saturated_fat_g=13, sodium_mg=20384,
            fibre_g=1.0, source_code="Fineli food 29009",
        ),
    },
    {
        "id": "frida:1253",
        "food_name": "Bouillon, beef, concentrated, cube",
        "source": "frida",
        "country": "DK",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://frida.fooddata.dk/food/1253?lang=en",
        "nutrients": _supplemental_nutrients(
            energy_kj=669, protein_g=17, carbohydrate_g=15.8, fat_g=3.2,
            sugars_g=3.95, saturated_fat_g=1.49, sodium_mg=22500,
            fibre_g=0, source_code="Frida food 1253",
        ),
    },
    {
        "id": "retail:knorr-fr-reduced-salt-chicken-cube",
        "food_name": "Stock cube, chicken, reduced salt",
        "source": "retail",
        "country": "FR",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.carrefour.fr/p/bouillon-de-poule-knorr-8720182886590",
        "nutrients": _supplemental_nutrients(
            energy_kj=1381, protein_g=8.1, carbohydrate_g=28, fat_g=21,
            sugars_g=9.2, saturated_fat_g=13, sodium_mg=11840,
            fibre_g=0.5, source_code="Knorr FR reduced-salt chicken cube label, per 100 g as sold",
        ),
    },
    {
        "id": "retail:knorr-fr-reduced-salt-vegetable-cube",
        "food_name": "Stock cube, vegetable, reduced salt",
        "source": "retail",
        "country": "FR",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.coursesu.com/p/cubes-de-bouillon-de-legumes-reduit-en-sel-knorr-12-tablettes-109g6l/7190298.html",
        "nutrients": _supplemental_nutrients(
            energy_kj=1410, protein_g=4.5, carbohydrate_g=40, fat_g=17,
            sugars_g=15, saturated_fat_g=12, sodium_mg=12440,
            fibre_g=2.3, source_code="Knorr FR reduced-salt vegetable cube label, per 100 g as sold",
        ),
    },
    {
        "id": "retail:knorr-fs-fr-chicken-bouillon-powder",
        "food_name": "Bouillon powder, chicken, dehydrated",
        "source": "retail",
        "country": "FR",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.unileverfoodsolutions.fr/produit/knorr-bouillon-de-volaille-deshydrate-seau-4kg-jusqu-a-200l-3-FR-687430.html",
        "nutrients": _supplemental_nutrients(
            energy_kj=1178, protein_g=8.2, carbohydrate_g=47, fat_g=6.4,
            sugars_g=8.8, saturated_fat_g=2.1, sodium_mg=13240,
            fibre_g=0.5, source_code="Unilever Food Solutions FR dehydrated chicken bouillon, per 100 g",
        ),
    },
    {
        "id": "retail:knorr-fr-chicken-stock-pot",
        "food_name": "Stock pot, chicken (gel)",
        "source": "retail",
        "country": "FR",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.knorr.com/fr/p/marmite-de-bouillon%C2%AE-poule.html/08722700277507",
        "nutrients": _supplemental_nutrients(
            energy_kj=389, protein_g=2.4, carbohydrate_g=4.8, fat_g=7.2,
            sugars_g=3.9, saturated_fat_g=4.0, sodium_mg=8480,
            fibre_g=0.8, source_code="Knorr FR chicken stock pot (gel) label, per 100 g",
        ),
    },
    {
        "id": "retail:knorr-fr-vegetable-stock-pot",
        "food_name": "Stock pot, vegetable (gel)",
        "source": "retail",
        "country": "FR",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.knorr.com/fr/p/marmite-de-bouillon%C2%AE-l%C3%A9gumes.html/08722700277446",
        "nutrients": _supplemental_nutrients(
            energy_kj=444, protein_g=2.3, carbohydrate_g=8.1, fat_g=7.0,
            sugars_g=6.9, saturated_fat_g=4.7, sodium_mg=8200,
            fibre_g=1.1, source_code="Knorr FR vegetable stock pot (gel) label, per 100 g",
        ),
    },
    {
        "id": "eu:generic-broth-stock-avg",
        "food_name": "Broth or stock, chicken or vegetable, prepared (average, generic)",
        "source": "frida+fineli",
        "country": "EU",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://frida.fooddata.dk/food/277?lang=en"
        " ; https://fineli.fi/fineli/en/elintarvikkeet/29026",
        "nutrients": _supplemental_nutrients(
            energy_kj=28.5, protein_g=0.3, carbohydrate_g=0.35, fat_g=0.45,
            sugars_g=0.15, saturated_fat_g=0.177, sodium_mg=428.4,
            fibre_g=0.05,
            source_code=(
                "Average of Frida (Denmark) food 277 (Bouillon, chicken, prepared) and "
                "Fineli (Finland) food 29026 (Vegetable bouillon, dissolved); used only "
                "when the parsed recipe text has an unresolved 'X or Y' stock/broth "
                "alternative and no specific type is recoverable"
            ),
        ),
    },
    {
        "id": "retail:knorr-be-finesse-fish-bouillon-cube",
        "food_name": "Bouillon cube, fish",
        "source": "retail",
        "country": "BE",
        "food_group": "Concentrated stock and bouillon",
        "source_url": "https://www.carrefour.be/nl/finesse-bouillon-vis-12-bouillonblokjes/06254012.html",
        "nutrients": _supplemental_nutrients(
            energy_kj=1063, protein_g=19, carbohydrate_g=16, fat_g=9.3,
            sugars_g=3.7, saturated_fat_g=6.3, sodium_mg=17280,
            fibre_g=0.6, source_code="Knorr Finesse fish cube, Carrefour BE, per 100 g unprepared (onbereid)",
        ),
    },
)


# --------------------------------------------------------------------------- #
# Mapping loader                                                              #
# --------------------------------------------------------------------------- #


def load_mapping() -> list[dict[str, str]]:
    with MAPPING_CSV.open() as f:
        return list(csv.DictReader(f))


def _build_indices(mapping: list[dict[str, str]]) -> tuple[
    dict[str, dict[str, str]],          # ciqual_col -> mapping row
    dict[tuple[str, str], dict[str, str]],  # (cofid_sheet, cofid_col) -> mapping row
    dict[str, dict[str, str]],          # nevo_code -> mapping row
]:
    ciqual_idx: dict[str, dict[str, str]] = {}
    cofid_idx: dict[tuple[str, str], dict[str, str]] = {}
    nevo_idx: dict[str, dict[str, str]] = {}
    for row in mapping:
        if row["ciqual_column"]:
            ciqual_idx[row["ciqual_column"]] = row
        if row["cofid_sheet"] and row["cofid_column"]:
            cofid_idx[(row["cofid_sheet"], row["cofid_column"])] = row
        if row["nevo_code"]:
            nevo_idx[row["nevo_code"]] = row
    return ciqual_idx, cofid_idx, nevo_idx


def _output_key(row: dict[str, str]) -> str:
    return row["usda_name"] or row["eu_canonical_name"]


def _output_unit(row: dict[str, str], native_unit: str) -> str:
    # Use USDA unit if defined (so the matcher sees identical units); else
    # carry the native unit normalised µg -> ug.
    if row["usda_unit"]:
        return row["usda_unit"]
    return (native_unit or "").replace("µg", "ug").strip()


# --------------------------------------------------------------------------- #
# Value coercion                                                              #
# --------------------------------------------------------------------------- #


def _to_float(value: Any) -> float | None:
    """Coerce a raw cell to float; return None for null-like values.

    USDA convention: missing nutrient = absent key (not 0).
    CoFID sentinels: 'Tr' (trace), 'N' (unknown), '' -> null.
    Ciqual sentinels: 'traces', '-', '' -> null.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"tr", "n", "traces", "-", "nd", "na", "n/a"}:
        return None
    # Ciqual sometimes uses '< 0.1' etc. — strip the prefix.
    if s.startswith("<") or s.startswith(">"):
        s = s[1:].strip()
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Vitamin A IU back-computation                                               #
# --------------------------------------------------------------------------- #


def _compute_vitamin_a_iu(nutrients: dict[str, dict[str, Any]]) -> None:
    """USDA pre-2001 formula: IU = retinol_ug / 0.3 + beta_carotene_ug / 0.6."""
    retinol = nutrients.get("Retinol", {}).get("value")
    beta = nutrients.get("Carotene, beta", {}).get("value")
    if retinol is None and beta is None:
        return
    value = (retinol or 0.0) / 0.3 + (beta or 0.0) / 0.6
    nutrients["Vitamin A, IU"] = {
        "unit": "IU",
        "value": round(value, 2),
        "source_code": "computed_from_retinol+beta_carotene",
    }


# --------------------------------------------------------------------------- #
# Ciqual                                                                      #
# --------------------------------------------------------------------------- #


def load_ciqual(ciqual_idx: dict[str, dict[str, str]]) -> Iterable[dict[str, Any]]:
    df = pd.read_excel(CIQUAL_XLSX, sheet_name="food composition")
    # Collapse multi-line headers to single-space strings (mapping uses cleaned form)
    df.columns = [" ".join(str(c).split()) for c in df.columns]

    before = len(df)
    df = df[~df["alim_grp_nom_eng"].isin(CIQUAL_DROP_GROUPS)].copy()
    dropped = before - len(df)
    print(f"[ciqual] {len(df)} foods kept ({dropped} dropped as composite dishes)")

    for _, row in df.iterrows():
        code = row.get("alim_code")
        name = row.get("alim_nom_eng")
        if pd.isna(code) or pd.isna(name):
            continue
        nutrients: dict[str, dict[str, Any]] = {}
        for col, m in ciqual_idx.items():
            if col not in df.columns:
                continue
            val = _to_float(row[col])
            if val is None:
                continue
            # native unit from parenthesised header e.g. "(g 100g)" / "(µg 100g)"
            native_unit = ""
            if "(" in col and ")" in col:
                inside = col.split("(")[-1].split(")")[0]
                native_unit = inside.split()[0]
            key = _output_key(m)
            if not key:
                continue
            entry: dict[str, Any] = {
                "unit": _output_unit(m, native_unit),
                "value": val,
                "source_code": col,
            }
            # Flag the Vit A RAE col (which Ciqual labels misleadingly as "100mg")
            nutrients[key] = entry
        _compute_vitamin_a_iu(nutrients)
        if not nutrients:
            continue
        yield {
            "id": f"ciqual:{int(code) if isinstance(code, (int, float)) and float(code).is_integer() else code}",
            "food_name": str(name).strip(),
            "source": "ciqual",
            "country": "FR",
            "food_group": str(row.get("alim_grp_nom_eng") or "").strip() or None,
            "nutrients": nutrients,
        }


# --------------------------------------------------------------------------- #
# CoFID                                                                       #
# --------------------------------------------------------------------------- #


def load_cofid(cofid_idx: dict[tuple[str, str], dict[str, str]]) -> Iterable[dict[str, Any]]:
    # Sheets we need (only the ones referenced in the mapping)
    needed_sheets = sorted({s for (s, _c) in cofid_idx.keys()})
    sheets: dict[str, pd.DataFrame] = {}
    for s in needed_sheets:
        df = pd.read_excel(COFID_XLSX, sheet_name=s, header=2)
        df.rename(columns={
            "Unnamed: 0": "_code",
            "Unnamed: 1": "_name",
            "Unnamed: 3": "_group",
        }, inplace=True)
        # Strip any whitespace-mangled column names in the mapping
        df.columns = [str(c).strip() for c in df.columns]
        df = df[df["_code"].astype(str).str.match(r"^\d+-\d+", na=False)]
        sheets[s] = df.set_index("_code", drop=False)

    primary = sheets[needed_sheets[0]]
    codes = primary["_code"].tolist()
    print(f"[cofid] {len(codes)} foods (no composite-dish filter applied — see TODO)")

    for code in codes:
        row0 = primary.loc[code]
        if isinstance(row0, pd.DataFrame):
            row0 = row0.iloc[0]
        name = row0.get("_name")
        group = row0.get("_group")
        if pd.isna(name):
            continue
        nutrients: dict[str, dict[str, Any]] = {}
        for (sheet, col), m in cofid_idx.items():
            sdf = sheets.get(sheet)
            if sdf is None or col not in sdf.columns or code not in sdf.index:
                continue
            r = sdf.loc[code]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[0]
            val = _to_float(r[col])
            if val is None:
                continue
            key = _output_key(m)
            if not key:
                continue
            nutrients[key] = {
                "unit": _output_unit(m, ""),
                "value": val,
                "source_code": f"{sheet}::{col}",
            }
        _compute_vitamin_a_iu(nutrients)
        if not nutrients:
            continue
        yield {
            "id": f"cofid:{code}",
            "food_name": str(name).strip(),
            "source": "cofid",
            "country": "UK",
            "food_group": str(group).strip() if not pd.isna(group) else None,
            "nutrients": nutrients,
        }


# --------------------------------------------------------------------------- #
# NEVO                                                                        #
# --------------------------------------------------------------------------- #


def load_nevo(nevo_idx: dict[str, dict[str, str]]) -> Iterable[dict[str, Any]]:
    # ``NA`` is NEVO's sodium nutrient code, so disable pandas' default NA parsing.
    df = pd.read_csv(NEVO_CSV, sep="|", keep_default_na=False, na_values=[""])
    df.rename(columns={
        "NEVO-code": "code",
        "Engelse naam/Food name": "food_name",
        "Food group": "food_group",
        "Nutrient-code": "nutrient_code",
        "Gehalte/Value": "value",
        "Eenheid/Unit": "unit",
    }, inplace=True)

    before_groups = df["food_group"].unique().tolist()
    df = df[~df["food_group"].isin(NEVO_DROP_GROUPS)]
    print(f"[nevo] groups kept: {len(df['food_group'].unique())}/{len(before_groups)} "
          f"(dropped: {NEVO_DROP_GROUPS & set(before_groups)})")

    by_food: dict[str, dict[str, Any]] = {}
    for _, r in df.iterrows():
        code = r["code"]
        nutrient_code = r["nutrient_code"]
        m = nevo_idx.get(nutrient_code)
        if not m:
            continue
        val = _to_float(r["value"])
        if val is None:
            continue
        key = _output_key(m)
        if not key:
            continue
        food = by_food.setdefault(code, {
            "id": f"nevo:{code}",
            "food_name": str(r["food_name"]).strip(),
            "source": "nevo",
            "country": "NL",
            "food_group": str(r["food_group"]).strip() or None,
            "nutrients": {},
        })
        food["nutrients"][key] = {
            "unit": _output_unit(m, r["unit"]),
            "value": val,
            "source_code": nutrient_code,
        }

    # NEVO has Lutein and Zeaxanthin separately; mapping sends lutein -> USDA
    # "Lutein + zeaxanthin". Sum in zeaxanthin if present (kept as EU-only key).
    for food in by_food.values():
        lz = food["nutrients"].get("Lutein + zeaxanthin")
        zea = food["nutrients"].get("Zeaxanthin")
        if lz and zea and lz.get("unit") == zea.get("unit"):
            lz["value"] = round(lz["value"] + zea["value"], 4)
            lz["source_code"] = "LUTN+ZEA"
        _compute_vitamin_a_iu(food["nutrients"])

    print(f"[nevo] {len(by_food)} foods")
    yield from by_food.values()


# --------------------------------------------------------------------------- #
# DB                                                                          #
# --------------------------------------------------------------------------- #


DDL = """
CREATE TABLE IF NOT EXISTS "{table}" (
    id text PRIMARY KEY,
    food_name text NOT NULL,
    source text NOT NULL,
    country text NOT NULL,
    food_group text,
    nutrients jsonb NOT NULL,
    source_url text
);
ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS source_url text;
CREATE INDEX IF NOT EXISTS "{table}_source_idx" ON "{table}"(source);
CREATE INDEX IF NOT EXISTS "{table}_food_name_idx" ON "{table}"(food_name);
"""

UPSERT = """
INSERT INTO "{table}" (id, food_name, source, country, food_group, nutrients, source_url)
VALUES (:id, :food_name, :source, :country, :food_group, CAST(:nutrients AS jsonb), :source_url)
ON CONFLICT (id) DO UPDATE SET
    food_name = EXCLUDED.food_name,
    source = EXCLUDED.source,
    country = EXCLUDED.country,
    food_group = EXCLUDED.food_group,
    nutrients = EXCLUDED.nutrients,
    source_url = EXCLUDED.source_url
"""


def upsert(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(DDL.format(table=TABLE)))
        params = [
            {**r, "nutrients": json.dumps(r["nutrients"]), "source_url": r.get("source_url")}
            for r in records
        ]
        conn.execute(text(UPSERT.format(table=TABLE)), params)


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Skip DB write; just report")
    ap.add_argument("--sample", type=int, default=0, help="Print N sample records")
    args = ap.parse_args()

    mapping = load_mapping()
    ciqual_idx, cofid_idx, nevo_idx = _build_indices(mapping)
    print(f"Mapping: {len(mapping)} rows ({len(ciqual_idx)} ciqual cols, "
          f"{len(cofid_idx)} cofid cols, {len(nevo_idx)} nevo codes)")

    records: list[dict[str, Any]] = []
    records.extend(load_ciqual(ciqual_idx))
    records.extend(load_cofid(cofid_idx))
    records.extend(load_nevo(nevo_idx))
    records.extend(SUPPLEMENTAL_FOODS)

    by_source: dict[str, int] = {}
    for r in records:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    print(f"\nTotal: {len(records)} foods")
    for s, n in sorted(by_source.items()):
        print(f"  {s}: {n}")

    if args.sample:
        print(f"\n--- {args.sample} samples ---")
        for r in records[:args.sample]:
            print(json.dumps(
                {k: (v if k != "nutrients" else {kk: vv for kk, vv in list(v.items())[:5]})
                 for k, v in r.items()},
                indent=2, ensure_ascii=False, default=str,
            ))

    if args.dry_run:
        print("\n[dry-run] skipping DB write")
        return

    print(f"\nUpserting into {TABLE}...")
    # batch in chunks of 500 to keep param sizes sane
    for i in range(0, len(records), 500):
        upsert(records[i:i + 500])
    print("Done.")


if __name__ == "__main__":
    main()
