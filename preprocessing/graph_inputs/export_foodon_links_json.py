import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import unquote


# Purpose: Export canonical ingredient -> FoodOn links from foodon-links.trig to JSON.

DEFAULT_INPUT = "./data/processed/foodkg/foodon-links.trig"
DEFAULT_PAIRS = "./data/processed/foodkg/foodon-pairs.csv"
DEFAULT_CANONICAL = "./data/processed/recipe1m/recipe1m_canonical_ingredients.json"
DEFAULT_OUTPUT = "./data/mappings/recipe1m-foodon-links.json"

LINK_RE = re.compile(
    r"<(http://idea\.rpi\.edu/heals/kb/ingredientname/[^>]+)>\s+"
    r"owl:equivalentClass\s+"
    r"<(http://purl\.obolibrary\.org/obo/FOODON_[^>]+)>"
)
INGREDIENT_URI_PREFIX = "http://idea.rpi.edu/heals/kb/ingredientname/"


def load_pairs(path: Path) -> dict[str, str]:
    pairs = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            resource = (row.get("Resource") or "").strip()
            label = (row.get("str") or "").strip()
            if resource:
                pairs[resource] = label
    return pairs


def load_canonical_map(path: Path) -> dict[str, dict[str, str]]:
    canonical = {}
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        name = (entry.get("canonical") or "").strip()
        canonical_id = entry.get("canonical_id")
        if name and canonical_id:
            canonical[name.casefold()] = {
                "canonical_id": canonical_id,
                "canonical": name,
            }
    return canonical


def ingredient_name_from_uri(uri: str) -> str | None:
    if not uri or not uri.startswith(INGREDIENT_URI_PREFIX):
        return None
    return unquote(uri[len(INGREDIENT_URI_PREFIX) :]).strip()


def foodon_id_from_uri(uri: str) -> str | None:
    if not uri:
        return None
    return uri.rsplit("/", 1)[-1] if "/" in uri else uri


def main():
    parser = argparse.ArgumentParser(
        description="Export ingredientname → FoodOn links from foodon-links.trig to JSON."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Path to foodon-links.trig (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--pairs",
        default=DEFAULT_PAIRS,
        help=f"Path to foodon-pairs.csv (default: {DEFAULT_PAIRS})",
    )
    parser.add_argument(
        "--canonical",
        default=DEFAULT_CANONICAL,
        help=f"Path to recipe1m_canonical_ingredients.json (default: {DEFAULT_CANONICAL})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    pairs_path = Path(args.pairs)
    canonical_path = Path(args.canonical)
    output_path = Path(args.output)

    pairs = load_pairs(pairs_path)
    canonical_map = load_canonical_map(canonical_path)
    text = input_path.read_text(encoding="utf-8", errors="ignore")
    links = LINK_RE.findall(text)

    rows = []
    for ingredient_uri, foodon_uri in links:
        ingredient_name = ingredient_name_from_uri(ingredient_uri)
        foodon_id = foodon_id_from_uri(foodon_uri)
        canonical = canonical_map.get((ingredient_name or "").casefold())
        rows.append(
            {
                "canonical_id": canonical.get("canonical_id") if canonical else None,
                "canonical": canonical.get("canonical") if canonical else ingredient_name,
                "foodon_id": foodon_id,
                "foodon_label": pairs.get(foodon_uri),
            }
        )

    rows.sort(key=lambda row: (row.get("canonical") or ""))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote {len(rows)} links to {output_path}")


if __name__ == "__main__":
    main()
