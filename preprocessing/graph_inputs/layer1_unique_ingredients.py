import argparse
import csv
import json
from pathlib import Path

# Purpose: Extract unique ingredient texts from layer1.json into a CSV.

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional dependency
    def tqdm(iterable, **_kwargs):
        return iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract unique ingredient texts from layer1.json into a CSV."
    )
    parser.add_argument(
        "--input",
        default="foodkg.github.io/src/verify/data/layer1.json",
        help="Path to layer1.json",
    )
    parser.add_argument(
        "--output",
        default="data/layer1_unique_ingredients.csv",
        help="Path to output CSV",
    )
    return parser.parse_args()


def iter_recipes(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "recipes" in payload and isinstance(payload["recipes"], list):
            return payload["recipes"]
        return payload.values()
    raise TypeError(
        "Unsupported JSON root type: {}".format(type(payload).__name__)
    )


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    with input_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    seen = set()
    unique = []

    for recipe in tqdm(iter_recipes(payload), desc="Scanning recipes"):
        for ingredient in recipe.get("ingredients", []):
            text = ingredient.get("text")
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            unique.append(text)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ingredient"])
        for item in unique:
            writer.writerow([item])

    print("Wrote {} unique ingredients to {}".format(len(unique), output_path))


if __name__ == "__main__":
    main()
