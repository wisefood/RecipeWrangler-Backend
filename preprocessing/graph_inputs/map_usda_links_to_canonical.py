import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import unquote

from tqdm import tqdm


# Purpose: Map USDA ingredient URIs to Recipe1M canonical ingredients and enrich with USDA labels/groups.

DEFAULT_LINKS = "./data/mappings/recipe1m-usda-links.json"
DEFAULT_CANONICAL = "./data/processed/recipe1m/recipe1m_canonical_ingredients.json"
DEFAULT_FOODKG_CORE = "./data/processed/foodkg/foodkg-core.trig"
DEFAULT_USDA_PAIRS = "./data/processed/foodkg/usda-pairs.csv"
DEFAULT_FOOD_DES = "./data/raw/usda/FOOD_DES.txt"
DEFAULT_FD_GROUP = "./data/raw/usda/FD_GROUP.txt"
DEFAULT_OUTPUT = "./data/mappings/recipe1m-usda-links-canonical.json"

INGREDIENT_URI_RE = re.compile(r"<(http://idea\.rpi\.edu/heals/kb/ingredientname/[^>]+)>")
INGREDIENT_URI_PREFIX = "http://idea.rpi.edu/heals/kb/ingredientname/"
USDA_URI_PREFIX = "http://idea.rpi.edu/heals/kb/usda#"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_usda_pairs(path: Path) -> dict[str, str]:
    pairs = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            resource = (row.get("Resource") or "").strip()
            label = (row.get("str") or "").strip()
            if resource:
                pairs[resource] = label
    return pairs


def parse_caret_fields(line: str) -> list[str]:
    return [field.strip().strip("~") for field in line.rstrip().split("^")]


def load_food_des(path: Path) -> dict[str, str]:
    food_to_group = {}
    with path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 2:
                continue
            food_id, group_id = fields[0], fields[1]
            if food_id and group_id:
                food_to_group[food_id] = group_id
    return food_to_group


def load_food_groups(path: Path) -> dict[str, str]:
    group_map = {}
    with path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 2:
                continue
            group_id, description = fields[0], fields[1]
            if group_id:
                group_map[group_id] = description
    return group_map


def extract_ingredient_uris(path: Path) -> set[str]:
    ingredient_uris = set()
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            for match in INGREDIENT_URI_RE.finditer(line):
                ingredient_uris.add(match.group(1))
    return ingredient_uris


def build_canonical_maps(canonical_entries: list[dict]):
    canonical_by_name = {}
    alias_to_canonical = {}
    for entry in canonical_entries:
        canonical = (entry.get("canonical") or "").strip()
        if canonical:
            canonical_by_name[canonical.casefold()] = entry
        for alias in entry.get("aliases", []):
            alias_text = (alias or "").strip()
            if not alias_text:
                continue
            alias_key = alias_text.casefold()
            alias_to_canonical.setdefault(alias_key, entry)
    return canonical_by_name, alias_to_canonical


def ingredient_name_from_uri(uri: str) -> str | None:
    if not uri or not uri.startswith(INGREDIENT_URI_PREFIX):
        return None
    return unquote(uri[len(INGREDIENT_URI_PREFIX) :]).strip()


def usda_id_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith(USDA_URI_PREFIX):
        return uri[len(USDA_URI_PREFIX) :]
    return None


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Replace ingredient URIs in recipe1m-usda-links.json with canonical ingredients "
            "from recipe1m_canonical_ingredients.json."
        )
    )
    parser.add_argument(
        "--links",
        default=DEFAULT_LINKS,
        help=f"Path to recipe1m-usda-links.json (default: {DEFAULT_LINKS})",
    )
    parser.add_argument(
        "--canonical",
        default=DEFAULT_CANONICAL,
        help=f"Path to recipe1m_canonical_ingredients.json (default: {DEFAULT_CANONICAL})",
    )
    parser.add_argument(
        "--foodkg-core",
        default=DEFAULT_FOODKG_CORE,
        help=f"Path to foodkg-core.trig (default: {DEFAULT_FOODKG_CORE})",
    )
    parser.add_argument(
        "--usda-pairs",
        default=DEFAULT_USDA_PAIRS,
        help=f"Path to usda-pairs.csv (default: {DEFAULT_USDA_PAIRS})",
    )
    parser.add_argument(
        "--food-des",
        default=DEFAULT_FOOD_DES,
        help=f"Path to FOOD_DES.txt (default: {DEFAULT_FOOD_DES})",
    )
    parser.add_argument(
        "--fd-group",
        default=DEFAULT_FD_GROUP,
        help=f"Path to FD_GROUP.txt (default: {DEFAULT_FD_GROUP})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write canonicalized USDA links (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--noncanonical-output",
        default=None,
        help="Optional path to write entries whose ingredient names are not canonical.",
    )
    parser.add_argument(
        "--missing-foodkg-output",
        default=None,
        help="Optional path to write ingredient URIs missing from foodkg-core.trig.",
    )
    args = parser.parse_args()

    links_path = Path(args.links)
    canonical_path = Path(args.canonical)
    foodkg_core_path = Path(args.foodkg_core)
    usda_pairs_path = Path(args.usda_pairs)
    food_des_path = Path(args.food_des)
    fd_group_path = Path(args.fd_group)
    output_path = Path(args.output)

    links = load_json(links_path)
    canonical_entries = load_json(canonical_path)
    canonical_by_name, alias_to_canonical = build_canonical_maps(canonical_entries)
    foodkg_ingredient_uris = extract_ingredient_uris(foodkg_core_path)
    usda_pairs = load_usda_pairs(usda_pairs_path)
    food_to_group = load_food_des(food_des_path)
    group_map = load_food_groups(fd_group_path)

    output_rows = []
    noncanonical_rows = []
    missing_foodkg_rows = []

    canonical_count = 0
    alias_count = 0
    missing_count = 0

    for row in tqdm(links, desc="Processing links"):
        ingredient_uri = row.get("ingredient_uri")
        usda_uri = row.get("usda_uri")
        usda_id = usda_id_from_uri(usda_uri)
        usda_food_label = usda_pairs.get(usda_uri)
        food_group_id = food_to_group.get(usda_id) if usda_id else None
        food_group = group_map.get(food_group_id) if food_group_id else None
        if not ingredient_uri:
            continue

        if ingredient_uri not in foodkg_ingredient_uris:
            missing_foodkg_rows.append(
                {
                    "ingredient_uri": ingredient_uri,
                    "usda_uri": usda_uri,
                    "usda_id": usda_id,
                    "usda_food_label": usda_food_label,
                    "food_group_id": food_group_id,
                    "food_group": food_group,
                }
            )

        ingredient_name = ingredient_name_from_uri(ingredient_uri)
        canonical_entry = None
        match_reason = "missing"
        if ingredient_name:
            key = ingredient_name.casefold()
            canonical_entry = canonical_by_name.get(key)
            if canonical_entry:
                match_reason = "canonical"
            else:
                canonical_entry = alias_to_canonical.get(key)
                if canonical_entry:
                    match_reason = "alias"

        if match_reason == "canonical":
            canonical_count += 1
        elif match_reason == "alias":
            alias_count += 1
        else:
            missing_count += 1

        canonical_id = canonical_entry.get("canonical_id") if canonical_entry else None
        canonical_name = canonical_entry.get("canonical") if canonical_entry else ingredient_name

        output_rows.append(
            {
                "canonical_id": canonical_id,
                "canonical": canonical_name,
                "usda_id": usda_id,
                "usda_food_label": usda_food_label,
                "food_group_id": food_group_id,
                "food_group": food_group,
            }
        )

        if match_reason != "canonical":
            noncanonical_rows.append(
                {
                    "ingredient_uri": ingredient_uri,
                    "ingredient_name": ingredient_name,
                    "usda_uri": usda_uri,
                    "usda_id": usda_id,
                    "usda_food_label": usda_food_label,
                    "food_group_id": food_group_id,
                    "food_group": food_group,
                    "match_reason": match_reason,
                    "canonical_id": canonical_id,
                    "canonical": canonical_name,
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_rows, indent=2, ensure_ascii=True), encoding="utf-8")

    if args.noncanonical_output:
        noncanonical_path = Path(args.noncanonical_output)
        noncanonical_path.parent.mkdir(parents=True, exist_ok=True)
        noncanonical_path.write_text(
            json.dumps(noncanonical_rows, indent=2, ensure_ascii=True), encoding="utf-8"
        )

    if args.missing_foodkg_output:
        missing_foodkg_path = Path(args.missing_foodkg_output)
        missing_foodkg_path.parent.mkdir(parents=True, exist_ok=True)
        missing_foodkg_path.write_text(
            json.dumps(missing_foodkg_rows, indent=2, ensure_ascii=True), encoding="utf-8"
        )

    total = len(links)
    print(f"total links: {total}")
    print(f"canonical matches: {canonical_count}")
    print(f"alias matches: {alias_count}")
    print(f"missing canonical matches: {missing_count}")
    print(f"missing in foodkg-core: {len(missing_foodkg_rows)}")
    print(f"output: {output_path}")
    if args.noncanonical_output:
        print(f"noncanonical report: {args.noncanonical_output}")
    if args.missing_foodkg_output:
        print(f"missing foodkg-core report: {args.missing_foodkg_output}")


if __name__ == "__main__":
    main()
