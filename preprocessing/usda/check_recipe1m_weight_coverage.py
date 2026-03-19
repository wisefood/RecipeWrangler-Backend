import argparse
import csv
import json
import sys
from pathlib import Path

from tqdm import tqdm

# Purpose: Find unique Recipe1M ingredients that do not get a USDA weight match.

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda  # noqa: E402


DEFAULT_INPUT = REPO_ROOT / "data/processed/recipe1m/recipe1m-ex-limited.json"
DEFAULT_OUTPUT = REPO_ROOT / "data/processed/recipe1m/recipe1m-unmatched-ingredient-weights.csv"


def stream_recipe1m(path: Path):
    decoder = json.JSONDecoder()
    buffer = ""
    in_array = False

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            buffer += chunk

            while True:
                if not in_array:
                    stripped = buffer.lstrip()
                    if not stripped:
                        break
                    if stripped[0] != "[":
                        raise ValueError("Expected a JSON array in recipe file.")
                    buffer = stripped[1:]
                    in_array = True

                buffer = buffer.lstrip()
                if not buffer:
                    break
                if buffer[0] == "]":
                    return
                if buffer[0] == ",":
                    buffer = buffer[1:]
                    continue

                try:
                    obj, idx = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break

                buffer = buffer[idx:]
                yield obj

        buffer = buffer.lstrip()
        if buffer and buffer[0] != "]":
            raise ValueError("Incomplete JSON array in recipe file.")


def _measurement_from_parts(quantity, unit) -> str:
    q = "" if quantity is None else str(quantity).strip()
    u = "" if unit is None else str(unit).strip()
    return f"{q} {u}".strip()


def collect_unique_ingredients(input_path: Path) -> list[tuple[str, str]]:
    unique: dict[str, str] = {}
    for recipe in tqdm(stream_recipe1m(input_path), desc="Reading recipes"):
        for ingredient in recipe.get("ingredients", []):
            if not isinstance(ingredient, dict):
                continue
            name_raw = ingredient.get("name")
            if name_raw is None:
                continue
            name = str(name_raw).strip().lower()
            if not name:
                continue
            if name in unique:
                continue
            unique[name] = _measurement_from_parts(
                ingredient.get("quantity"), ingredient.get("unit")
            )
    return list(unique.items())


def run_weight_check(unique_items: list[tuple[str, str]], batch_size: int) -> list[dict]:
    unmatched: list[dict] = []
    for i in tqdm(range(0, len(unique_items), batch_size), desc="Checking weights"):
        batch = unique_items[i : i + batch_size]
        names = [item[0] for item in batch]
        measurements = [item[1] for item in batch]

        result = ingredient_weight_tool_usda.invoke(
            {
                "ingredient_names": names,
                "measurements": measurements,
                "return_details": True,
            }
        )
        details = result.get("details", []) if isinstance(result, dict) else []

        for item, detail in zip(batch, details):
            if not isinstance(detail, dict):
                continue
            if detail.get("weight_grams") is not None:
                continue
            unmatched.append(
                {
                    "ingredient": item[0],
                    "sample_measurement": item[1],
                    "error": detail.get("error"),
                    "usda_id": detail.get("usda_id"),
                    "parsed_quantity": detail.get("parsed_quantity"),
                    "parsed_unit": detail.get("parsed_unit"),
                    "match_type": detail.get("match_type"),
                }
            )
    return unmatched


def write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ingredient",
        "sample_measurement",
        "error",
        "usda_id",
        "parsed_quantity",
        "parsed_unit",
        "match_type",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "List unique Recipe1M ingredients that fail USDA weight calculation."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    unique_items = collect_unique_ingredients(args.input)
    print(f"Unique ingredients found: {len(unique_items)}")

    unmatched = run_weight_check(unique_items, args.batch_size)
    write_csv(unmatched, args.output)

    print(f"Unmatched ingredients: {len(unmatched)}")
    print(f"Output CSV: {args.output}")


if __name__ == "__main__":
    main()
