"""Export Hungarian composition XLSX into normalized CSV for runtime ingestion."""

from __future__ import annotations

import argparse
import csv
import re
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    REPO_ROOT
    / "data"
    / "processed"
    / "hungarian-comp-table"
    / "Hungary_FoodCompositionTable_Edited.xlsx"
)
DEFAULT_OUTPUT = (
    REPO_ROOT / "data" / "processed" / "hungarian-comp-table" / "hungarian_comp_table.csv"
)

OUTPUT_COLUMNS = [
    "canonical_food_id",
    "Food Name",
    "Category",
    "Energy (kJ) (kJ)",
    "Energy (kcal) (kcal)",
    "Protein (g)",
    "Fat (g)",
    "Carbohydrate (g)",
    "Sodium (mg)",
    "Potassium (mg)",
    "Calcium (mg)",
    "Magnesium (mg)",
    "Retinol equiv ug",
    "Vitamin E mg",
]


def _normalize_header(text: str) -> str:
    compact = re.sub(r"\s+", "", (text or "").replace("\n", " ")).strip().lower()
    return re.sub(r"[^a-z0-9]", "", compact)


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in (cell_ref or "") if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(path))
    out: list[str] = []
    for si in root.findall(f"{NS_MAIN}si"):
        out.append("".join((t.text or "") for t in si.iter(f"{NS_MAIN}t")))
    return out


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value_node = cell.find(f"{NS_MAIN}v")
    if value_node is None:
        return ""
    value = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except Exception:
            return value
    return value


def _rows_from_xlsx(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = workbook.find(f"{NS_MAIN}sheets")
        if sheets is None or len(list(sheets)) == 0:
            raise RuntimeError("Workbook has no sheets.")

        first_sheet = list(sheets)[0]
        rel_id = first_sheet.attrib.get(f"{NS_REL}id")
        if not rel_id:
            raise RuntimeError("Missing sheet relation id.")

        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib.get("Id"): rel.attrib.get("Target") for rel in rels}
        target = rel_map.get(rel_id)
        if not target:
            raise RuntimeError(f"Missing relation target for {rel_id}.")

        sheet_path = "xl/" + target.lstrip("/")
        ws = ET.fromstring(zf.read(sheet_path))
        shared_strings = _load_shared_strings(zf)

        rows: list[list[str]] = []
        for row in ws.findall(f".//{NS_MAIN}row"):
            values: list[str] = []
            for cell in row.findall(f"{NS_MAIN}c"):
                idx = _column_index(cell.attrib.get("r", ""))
                while len(values) <= idx:
                    values.append("")
                values[idx] = _cell_value(cell, shared_strings)
            rows.append(values)
        return rows


def _clean_numeric(raw: object) -> str:
    text = str(raw or "").strip()
    if not text or text == "-":
        return ""
    text = text.replace(",", ".")
    return text


def _to_output_row(row: dict[str, str], index: int) -> dict[str, str]:
    return {
        "canonical_food_id": f"HU{index:05d}",
        "Food Name": str(row.get("Foods, raw materials 100 g") or "").strip(),
        "Category": str(row.get("Category") or "").strip(),
        "Energy (kJ) (kJ)": _clean_numeric(row.get("Energy kJ")),
        "Energy (kcal) (kcal)": _clean_numeric(row.get("Energy kcal")),
        "Protein (g)": _clean_numeric(row.get("Protein g")),
        "Fat (g)": _clean_numeric(row.get("Fat g")),
        "Carbohydrate (g)": _clean_numeric(row.get("Carbohydrates g")),
        "Sodium (mg)": _clean_numeric(row.get("Sodium mg")),
        "Potassium (mg)": _clean_numeric(row.get("Potassium mg")),
        "Calcium (mg)": _clean_numeric(row.get("Calcium mg")),
        "Magnesium (mg)": _clean_numeric(row.get("Magnesium mg")),
        "Retinol equiv ug": _clean_numeric(row.get("Retinol equiv. ug")),
        "Vitamin E mg": _clean_numeric(row.get("Vitamin E mg")),
    }


def export_csv(input_path: Path, output_path: Path) -> int:
    raw_rows = _rows_from_xlsx(input_path)
    if not raw_rows:
        raise RuntimeError(f"No rows found in workbook: {input_path}")

    header = raw_rows[0]
    normalized = {_normalize_header(h): i for i, h in enumerate(header) if str(h).strip()}

    aliases = {
        "category": "Category",
        "foodsrawmaterials100g": "Foods, raw materials 100 g",
        "energykj": "Energy kJ",
        "energykcal": "Energy kcal",
        "proteing": "Protein g",
        "fatg": "Fat g",
        "carbohydratesg": "Carbohydrates g",
        "sodiummg": "Sodium mg",
        "potassiummg": "Potassium mg",
        "calciummg": "Calcium mg",
        "magnesiummg": "Magnesium mg",
        "retinolequivg": "Retinol equiv. ug",
        "vitaminemg": "Vitamin E mg",
    }

    index_by_alias: dict[str, int] = {}
    for norm, alias in aliases.items():
        if norm in normalized:
            index_by_alias[alias] = normalized[norm]

    required_aliases = ["Foods, raw materials 100 g"]
    missing = [a for a in required_aliases if a not in index_by_alias]
    if missing:
        raise RuntimeError(f"Missing required columns in workbook: {missing}")

    records: list[dict[str, str]] = []
    running_id = 1
    for values in raw_rows[1:]:
        row: dict[str, str] = {}
        for alias, idx in index_by_alias.items():
            row[alias] = values[idx] if idx < len(values) else ""
        food_name = str(row.get("Foods, raw materials 100 g") or "").strip()
        if not food_name:
            continue
        records.append(_to_output_row(row, running_id))
        running_id += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(records)

    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize Hungarian composition table XLSX into CSV for Postgres/Chroma ingestion."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    count = export_csv(args.input, args.output)
    print(f"exported_rows={count} output={args.output}")


if __name__ == "__main__":
    main()
