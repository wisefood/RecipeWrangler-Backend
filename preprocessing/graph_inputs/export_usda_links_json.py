import argparse
import json
import re
from pathlib import Path


# Purpose: Export ingredient -> USDA links from usda-links.trig to JSON.

DEFAULT_INPUT = "../foodkg.github.io/src/verify/data/usda-links.trig"
DEFAULT_OUTPUT = "../data/recipe1m-usda-links.json"


LINK_RE = re.compile(
    r"<(http://idea\.rpi\.edu/heals/kb/ingredientname/[^>]+)>\s+"
    r"owl:equivalentClass\s+"
    r"<(http://idea\.rpi\.edu/heals/kb/usda#[^>]+)>"
)


def parse_links(text):
    pairs = set(LINK_RE.findall(text))
    rows = [{"ingredient_uri": ing, "usda_uri": usda} for ing, usda in pairs]
    rows.sort(key=lambda row: row["ingredient_uri"])
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Export ingredientname → USDA links from usda-links.trig to JSON."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Path to usda-links.trig (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    text = input_path.read_text(encoding="utf-8", errors="ignore")
    rows = parse_links(text)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Wrote {len(rows)} links to {output_path}")


if __name__ == "__main__":
    main()
