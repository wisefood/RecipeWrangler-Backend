"""Normalize HealthyFoods raw JSON and assign deterministic recipe IDs."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path("data/HealthyFoods/HealthyFood_recipes.json")
DEFAULT_OUTPUT = Path("data/HealthyFoods/HealthyFood_recipes_clean.json")


# healthyfood.com renders an inline regional-name tooltip (e.g. "corn
# (sweetcorn)") whose trigger element leaks into scraped text as a bare "X"
# token, sometimes with an orphaned "s" from a stripped "(s)" plural marker
# right next to it (e.g. "eggplant aubergine X s" -> "eggplant aubergine").
# Verified: a standalone "s" token never appears in this source's ingredient
# text without the "X" artifact next to it, so both are safe to strip together.
_SYNONYM_TOOLTIP_ARTIFACT_RE = re.compile(r"(?<!\S)[Xs](?!\S)")


def _strip_synonym_tooltip_artifact(text: str) -> str:
    cleaned = _SYNONYM_TOOLTIP_ARTIFACT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip(" ,")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _as_clean_list(value: Any, *, strip_synonym_artifact: bool = False) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str) and value.strip():
        items = [value.strip()]
    else:
        items = []
    if strip_synonym_artifact:
        items = [_strip_synonym_tooltip_artifact(item) for item in items]
        items = [item for item in items if item]
    return items


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _duration_from_minutes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(float(value))
    text = str(value).strip().lower()
    if not text:
        return None

    # Handle common ranges like "20-30 min" -> 25
    range_match = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if range_match:
        low = int(range_match.group(1))
        high = int(range_match.group(2))
        return int(round((low + high) / 2))

    # Handle formats like "1 hr 20 min", "2h", "45m"
    hour_match = re.search(r"(\d+)\s*(h|hr|hrs|hour|hours)", text)
    min_match = re.search(r"(\d+)\s*(m|min|mins|minute|minutes)", text)
    if hour_match or min_match:
        hours = int(hour_match.group(1)) if hour_match else 0
        minutes = int(min_match.group(1)) if min_match else 0
        total = hours * 60 + minutes
        return total if total > 0 else None

    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        return int(float(match.group(0)))
    return None


def _serves_to_number(value: Any) -> int | None:
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


def _normalize_source(value: Any) -> str:
    source = _clean_text(value) or "HealthyFoods"
    if source.lower() in {"helathyfoods", "healthy foods", "healthyfood"}:
        return "HealthyFoods"
    return source


def _seed_for_recipe(title: str, recipe: dict[str, Any]) -> str:
    return (
        _clean_text(recipe.get("url"))
        or _clean_text(recipe.get("title"))
        or _clean_text(recipe.get("id"))
        or _clean_text(recipe.get("recipe_id"))
        or _clean_text(title)
        or "healthyfoods_recipe"
    ).lower()


def _candidate_from_seed(seed: str) -> int:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return int(digest, 16) % (10**10)


def _next_unique_id(seed: str, used: set[str]) -> str:
    num = _candidate_from_seed(seed)
    for _ in range(10**10):
        candidate = f"{num:010d}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        num = (num + 1) % (10**10)
    raise RuntimeError("Unable to allocate unique 10-digit recipe id.")


def normalize_recipe(title_key: str, recipe: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    title = _clean_text(recipe.get("title")) or title_key
    notes = _dedupe_keep_order(_as_clean_list(recipe.get("tips")) + _as_clean_list(recipe.get("variations")))

    clean = {
        "recipe_id": recipe_id,
        "id": recipe_id,
        "duration": _duration_from_minutes(recipe.get("time_minutes")),
        "image_url": _clean_text(recipe.get("image_url")),
        "instructions": _as_clean_list(recipe.get("instructions"), strip_synonym_artifact=True),
        "serves": _serves_to_number(recipe.get("serves")),
        "title": title,
        "url": _clean_text(recipe.get("link")),
        "description": _clean_text(recipe.get("description")),
        "ingredients": _as_clean_list(recipe.get("ingredients"), strip_synonym_artifact=True),
        "notes": notes,
        "categories": _as_clean_list(recipe.get("badge_tags")),
        "source": _normalize_source(recipe.get("source")),
    }
    return clean


def transform(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("Expected top-level JSON object keyed by recipe title")

    out: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    for title_key, recipe in data.items():
        if not isinstance(recipe, dict):
            continue
        recipe_id = _next_unique_id(_seed_for_recipe(str(title_key), recipe), used_ids)
        clean_recipe = normalize_recipe(str(title_key), recipe, recipe_id=recipe_id)
        out[clean_recipe["title"]] = clean_recipe
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform HealthyFoods recipes JSON into a clean schema.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to source HealthyFoods JSON")
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
