import argparse
import json
from pathlib import Path


# Purpose: Convert USDA NUTR_DEF/NUT_DATA into JSON nutrient rows with units.

DEFAULT_NUTR_DEF = "./data/raw/usda/NUTR_DEF.txt"
DEFAULT_NUT_DATA = "./data/raw/usda/NUT_DATA.txt"
DEFAULT_OUTPUT = "./data/processed/usda/usda-nutrients.json"


def normalize_unit(unit: str) -> str:
    if not unit:
        return unit
    return (
        unit.replace("µg", "ug")
        .replace("\u00b5g", "ug")
        .replace("\ufffdg", "ug")
        .replace("�g", "ug")
    )


def parse_caret_fields(line: str) -> list[str]:
    return [field.strip().strip("~") for field in line.rstrip().split("^")]


def load_nutr_def(path: Path) -> dict[str, dict[str, str]]:
    nutrients = {}
    with path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 4:
                continue
            nutr_id = fields[0]
            unit = normalize_unit(fields[1])
            description = fields[3]
            if nutr_id:
                nutrients[nutr_id] = {
                    "nutrient_description": description,
                    "unit": unit,
                }
    return nutrients


def parse_value(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Export USDA NUT_DATA with nutrient description and units."
    )
    parser.add_argument(
        "--nutr-def",
        default=DEFAULT_NUTR_DEF,
        help=f"Path to NUTR_DEF.txt (default: {DEFAULT_NUTR_DEF})",
    )
    parser.add_argument(
        "--nutr-data",
        default=DEFAULT_NUT_DATA,
        help=f"Path to NUT_DATA.txt (default: {DEFAULT_NUT_DATA})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    nutr_def_path = Path(args.nutr_def)
    nutr_data_path = Path(args.nutr_data)
    output_path = Path(args.output)

    nutr_defs = load_nutr_def(nutr_def_path)
    rows = []

    with nutr_data_path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 3:
                continue
            food_id = fields[0]
            nutrient_id = fields[1]
            nutrient_value = parse_value(fields[2])
            nutr_meta = nutr_defs.get(nutrient_id, {})

            rows.append(
                {
                    "food_id": food_id,
                    "nutrient_id": nutrient_id,
                    "nutrient_value": nutrient_value,
                    "nutrient_description": nutr_meta.get("nutrient_description"),
                    "unit": nutr_meta.get("unit"),
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
