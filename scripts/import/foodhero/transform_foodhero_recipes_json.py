"""Normalize scraped FoodHero JSON into the schema consumed by importers."""

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path("data/FoodHero/foodhero_recipes.json")
DEFAULT_OUTPUT = Path("data/FoodHero/foodhero_recipes_clean.json")


def _as_clean_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _pick_first_non_empty(recipe: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = recipe.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _build_duration(recipe: dict[str, Any]) -> str | None:
    prep = recipe.get("prep_time")
    cook = recipe.get("cook_time")

    prep = prep.strip() if isinstance(prep, str) else prep
    cook = cook.strip() if isinstance(cook, str) else cook

    if prep and cook:
        return f"prep: {prep}; cook: {cook}"
    if prep:
        return str(prep)
    if cook:
        return str(cook)
    return None


def normalize_recipe(title_key: str, recipe: dict[str, Any]) -> dict[str, Any]:
    title = _pick_first_non_empty(recipe, ["title"]) or title_key
    url = _pick_first_non_empty(recipe, ["url", "canonical_url", "source_url"])
    # A nutrition label may provide real serving count while the page's
    # "Makes" value is only a volume yield (for example 1½ cups of salsa).
    serves = _pick_first_non_empty(recipe, ["servings", "serves", "makes", "recipe_yield"])

    clean = {
        "duration": _build_duration(recipe),
        "image_url": _pick_first_non_empty(recipe, ["image_url"]),
        "instructions": _as_clean_list(_pick_first_non_empty(recipe, ["directions", "instructions"])),
        "serves": str(serves).strip() if serves is not None else None,
        "title": str(title).strip(),
        "url": str(url).strip() if url is not None else None,
        "ingredients": _as_clean_list(recipe.get("ingredients")),
        "notes": _as_clean_list(recipe.get("notes")),
        "categories": _as_clean_list(recipe.get("categories")),
        "source": _pick_first_non_empty(recipe, ["source"]) or "FoodHero",
    }
    return clean


def transform(data: Any) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = []
        for idx, recipe in enumerate(data):
            if not isinstance(recipe, dict):
                continue
            title = str(recipe.get("title") or f"recipe_{idx + 1}")
            items.append((title, recipe))
    else:
        raise ValueError("Unsupported JSON shape. Expected object or list.")

    out: dict[str, dict[str, Any]] = {}
    for title_key, recipe in items:
        if not isinstance(recipe, dict):
            continue
        clean_recipe = normalize_recipe(str(title_key), recipe)
        out[clean_recipe["title"]] = clean_recipe
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform FoodHero recipes JSON into a clean schema.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to source FoodHero JSON")
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
