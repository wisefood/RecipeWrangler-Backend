"""Normalize scraped MyPlate JSON into the canonical importer schema."""

import argparse
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path("data/MyPlate/myplate_recipes.json")
DEFAULT_OUTPUT = Path("data/MyPlate/myplate_recipes_clean.json")

FEDERAL_SITE_BANNER = (
    "Federal government websites often end in .gov or .mil. "
    "Before sharing sensitive information, make sure you’re on a federal government site."
)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _as_clean_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        return None
    match = re.search(r"\d+", text)
    if match:
        return int(match.group(0))
    return None


def _duration_from_title(title: str | None) -> int | None:
    if not title:
        return None
    match = re.search(r"\b(\d+)\s*-\s*minute\b|\b(\d+)\s*minute\b", title, flags=re.I)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _clean_description(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if text == FEDERAL_SITE_BANNER:
        return None
    return text


def normalize_recipe(title_key: str, recipe: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(recipe.get("title")) or title_key
    clean = {
        "duration": _duration_from_title(title),
        "image_url": _clean_text(recipe.get("image_url")),
        "instructions": _as_clean_list(recipe.get("directions") or recipe.get("instructions")),
        "serves": _to_int(recipe.get("servings") if "servings" in recipe else recipe.get("serves")),
        "title": title,
        "url": _clean_text(recipe.get("url")),
        "description": _clean_description(recipe.get("description")),
        "ingredients": _as_clean_list(recipe.get("ingredients")),
        "notes": _as_clean_list(recipe.get("notes")),
        "categories": [],
        "source": _clean_text(recipe.get("source")) or "MyPlate",
    }
    return clean


def transform(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("Expected top-level JSON object keyed by recipe title")

    out: dict[str, dict[str, Any]] = {}
    for title_key, recipe in data.items():
        if not isinstance(recipe, dict):
            continue
        clean_recipe = normalize_recipe(str(title_key), recipe)
        out[str(title_key)] = clean_recipe
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform MyPlate recipes JSON into a clean schema.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to source MyPlate JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path to cleaned output JSON")
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    cleaned = transform(raw)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(cleaned)} recipes -> {args.output}")


if __name__ == "__main__":
    main()
