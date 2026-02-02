import argparse
import json
import sys
from pathlib import Path

from tqdm import tqdm

# Purpose: Compute per-recipe USDA nutrition/score for Recipe1M and export JSON.

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.recipe_nutri_score import recipe_nutrition_and_score  # noqa: E402


INPUT_PATH = REPO_ROOT / "data/processed/recipe1m/recipe1m-ex-limited.json"
OUTPUT_PATH = REPO_ROOT / "data/processed/recipe1m/usda-recipes-nutrition.json"


def _write_batch(handle, batch, first_item):
    for item in batch:
        if not first_item:
            handle.write(",\n")
        handle.write(json.dumps(item, ensure_ascii=True))
        first_item = False
    handle.flush()
    return first_item


def main() -> None:
    parser = argparse.ArgumentParser(description="Export USDA nutrition for Recipe1M.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Flush results to disk every N recipes.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=INPUT_PATH,
        help="Input Recipe1M JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Output JSON file.",
    )
    args = parser.parse_args()

    recipes = json.loads(args.input.read_text(encoding="utf-8"))
    batch = []
    first_item = True
    with args.output.open("w", encoding="utf-8") as handle:
        handle.write("[\n")
        for recipe in tqdm(recipes, desc="Scoring recipes"):
            recipe_id = recipe.get("id")
            if not recipe_id:
                continue
            batch.append(recipe_nutrition_and_score(recipe_id))
            if len(batch) >= args.batch_size:
                first_item = _write_batch(handle, batch, first_item)
                batch.clear()
        if batch:
            first_item = _write_batch(handle, batch, first_item)
        handle.write("\n]\n")


if __name__ == "__main__":
    main()
