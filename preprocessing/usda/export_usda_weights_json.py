import argparse
import json
from pathlib import Path


# Purpose: Convert USDA WEIGHT.txt portions into JSON.

DEFAULT_INPUT = "./data/raw/usda/WEIGHT.txt"
DEFAULT_OUTPUT = "./data/processed/usda/usda-weights.json"


def parse_weight_line(line: str):
    fields = [field.strip().strip("~") for field in line.strip().split("^")]
    if len(fields) < 5:
        return None
    food_id, portion_seq, amount, portion_desc, grams = fields[:5]
    try:
        portion_seq_value = int(portion_seq)
    except ValueError:
        portion_seq_value = None
    try:
        amount_value = float(amount)
    except ValueError:
        amount_value = None
    try:
        grams_value = float(grams)
    except ValueError:
        grams_value = None
    return {
        "food_id": food_id,
        "portion_seq": portion_seq_value,
        "amount": amount_value,
        "portion_desc": portion_desc,
        "grams": grams_value,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Export USDA SR Legacy WEIGHT.txt portions to JSON."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Path to WEIGHT.txt (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    rows = []
    with input_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip():
                continue
            entry = parse_weight_line(line)
            if entry:
                rows.append(entry)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
