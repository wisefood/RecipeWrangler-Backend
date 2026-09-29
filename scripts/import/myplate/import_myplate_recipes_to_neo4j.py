"""Import normalized, canonicalized MyPlate recipes into the current graph schema."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from dotenv import load_dotenv

try:
    from tqdm import tqdm
except Exception:
    tqdm = None


DEFAULT_INPUT = Path("myplate_recipes_with_canonical_ingredients.json")
REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")


def _driver():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j"))
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError("Missing NEO4J_PASSWORD (or set NEO4J_NO_AUTH=1).")
    return GraphDatabase.driver(uri, auth=(username, password))


def _run(tx, query: str, params: dict[str, Any] | None = None) -> None:
    tx.run(query, params or {})


def _ensure_constraints(session) -> None:
    session.execute_write(
        _run,
        "CREATE CONSTRAINT recipe_recipe_id IF NOT EXISTS "
        "FOR (r:Recipe) REQUIRE r.recipe_id IS UNIQUE",
    )
    session.execute_write(
        _run,
        "CREATE CONSTRAINT ingredients_original_id IF NOT EXISTS "
        "FOR (o:Ingredients_original) REQUIRE o.original_id IS UNIQUE",
    )
    session.execute_write(
        _run,
        "CREATE INDEX ingredient_name_idx IF NOT EXISTS "
        "FOR (i:Ingredient) ON (i.name)",
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


_UNICODE_FRACTIONS = {
    "¼": 0.25,
    "½": 0.5,
    "¾": 0.75,
    "⅐": 1 / 7,
    "⅑": 1 / 9,
    "⅒": 0.1,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "⅕": 0.2,
    "⅖": 0.4,
    "⅗": 0.6,
    "⅘": 0.8,
    "⅙": 1 / 6,
    "⅚": 5 / 6,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
}


def _parse_numeric_token(token: str) -> float | None:
    t = (token or "").strip()
    if not t:
        return None
    if t in _UNICODE_FRACTIONS:
        return _UNICODE_FRACTIONS[t]
    try:
        return float(t)
    except Exception:
        pass
    if "/" in t:
        parts = t.split("/", 1)
        if len(parts) == 2:
            try:
                num = float(parts[0].strip())
                den = float(parts[1].strip())
                if den != 0:
                    return num / den
            except Exception:
                return None
    return None


def _parse_quantity(text: str) -> float | None:
    s = (text or "").strip()
    if not s:
        return None
    mixed = re.match(r"^\s*(\d+(?:\.\d+)?)\s+([0-9]+/[0-9]+|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])\b", s)
    if mixed:
        whole = float(mixed.group(1))
        frac = _parse_numeric_token(mixed.group(2)) or 0.0
        return whole + frac
    first = re.search(r"(\d+(?:\.\d+)?|[0-9]+/[0-9]+|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", s)
    if first:
        return _parse_numeric_token(first.group(1))
    return None


def _parse_serves(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_quantity(str(value))


def _parse_duration_minutes(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    if "varies" in s:
        return 0.0
    normalized = (
        s.replace("–", "-")
        .replace("—", "-")
        .replace(" to ", "-")
        .replace("mins", "minutes")
        .replace("min", "minute")
        .replace("hrs", "hours")
        .replace("hr", "hour")
    )
    total = 0.0
    found = False
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*(hour|hours|minute|minutes)\b",
        normalized,
    ):
        low = float(match.group(1))
        high = float(match.group(2)) if match.group(2) is not None else low
        value_num = (low + high) / 2.0
        if match.group(3).startswith("hour"):
            value_num *= 60.0
        total += value_num
        found = True
    if found:
        return total
    parsed = _parse_quantity(normalized)
    return parsed if parsed is not None else 0.0


def _iter_items(data: dict[str, Any]):
    items = list(data.items())
    if tqdm is not None:
        yield from tqdm(items, total=len(items), desc="Import MyPlate", unit="recipe")
        return
    total = len(items)
    for i, item in enumerate(items, start=1):
        print(f"[{i}/{total}] importing")
        yield item


def _merge_recipe(tx, recipe_id: str, recipe: dict[str, Any]) -> None:
    q = """
    MERGE (r:Recipe {recipe_id: $recipe_id})
    SET r.title = $title,
        r.url = $url,
        r.image_url = $image_url,
        r.instructions = $instructions,
        r.source = 'MyPlate',
        r.status = 'active',
        r.duration = $duration,
        r.serves = $serves
    """
    duration_raw = recipe.get("duration")
    serves_raw = recipe.get("serves")
    duration = _parse_duration_minutes(duration_raw)
    serves = _parse_serves(serves_raw)
    params = {
        "recipe_id": recipe_id,
        "title": _as_str(recipe.get("title")),
        "url": _as_str(recipe.get("url")),
        "image_url": _as_str(recipe.get("image_url")),
        "instructions": recipe.get("instructions") or [],
        "duration": duration if duration is not None else 0.0,
        "serves": serves if serves is not None else 0.0,
    }
    tx.run(q, params)


def _merge_original_and_canonical(
    tx,
    recipe_id: str,
    position: int,
    original_text: str | None,
    ci: dict[str, Any],
) -> None:
    canonical_name = _as_str(ci.get("match")) or _as_str(ci.get("name"))
    if not canonical_name:
        canonical_name = _as_str(original_text)
    canonical_name = canonical_name or f"unknown_{recipe_id}_{position}"

    q = """
    MATCH (r:Recipe {recipe_id: $recipe_id})
    MERGE (o:Ingredients_original {original_id: $original_id})
    SET o.name = $original_name,
        o.original_text = $original_text,
        o.source = 'MyPlate',
        o.status = 'active'
    MERGE (r)-[h:HAS_INGREDIENT_ORIGINAL {position: $position}]->(o)
    SET h.measurement = $measurement,
        h.unit = $unit

    MERGE (c:Ingredient {name: $canonical_name})
    ON CREATE SET
        c.canonical_id = coalesce($canonical_id, randomUUID()),
        c.source = 'MyPlate',
        c.status = 'resolved'
    ON MATCH SET
        c.canonical_id = coalesce(c.canonical_id, $canonical_id, randomUUID()),
        c.source = coalesce(c.source, 'MyPlate'),
        c.status = coalesce(c.status, 'resolved')

    MERGE (o)-[m:MAPS_TO]->(c)
    SET m.measurement = $measurement,
        m.unit = $unit,
        m.weight_grams = $weight_grams

    MERGE (r)-[hi:HAS_INGREDIENT]->(c)
    SET hi.measurement = $measurement,
        hi.unit = $unit,
        hi.weight_grams = $weight_grams
    """

    params = {
        "recipe_id": recipe_id,
        "original_id": f"{recipe_id}:{position}",
        "position": position,
        "original_name": _as_str(ci.get("name")) or _as_str(original_text),
        "original_text": _as_str(original_text),
        "measurement": _as_str(ci.get("measurement")),
        "unit": _as_str(ci.get("parsed_unit")),
        "canonical_name": canonical_name,
        "canonical_id": _as_str(ci.get("canonical_id")),
        "weight_grams": _as_float(ci.get("weight_grams")),
    }
    tx.run(q, params)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import MyPlate recipes + original ingredients into Neo4j and "
            "map to canonical Ingredient nodes."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input JSON file path")
    args = parser.parse_args()

    input_path = args.input if args.input.is_absolute() else Path.cwd() / args.input
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Input JSON must be an object keyed by recipe title.")

    driver = _driver()
    try:
        with driver.session() as session:
            _ensure_constraints(session)

            for key, recipe in _iter_items(data):
                if not isinstance(recipe, dict):
                    continue

                recipe_id = (
                    _as_str(recipe.get("recipe_id"))
                    or _as_str(recipe.get("id"))
                    or _as_str(key)
                )
                if not recipe_id:
                    continue

                session.execute_write(_merge_recipe, recipe_id, recipe)

                original_ingredients = recipe.get("ingredients") or []
                canonical_ingredients = recipe.get("canonical_ingredients") or []

                # Link every original row. If canonical rows are shorter/empty, fallback on original text.
                row_count = max(len(original_ingredients), len(canonical_ingredients))
                for idx in range(row_count):
                    original_text = original_ingredients[idx] if idx < len(original_ingredients) else None
                    ci = canonical_ingredients[idx] if idx < len(canonical_ingredients) else {}
                    if not isinstance(ci, dict):
                        ci = {}
                    session.execute_write(
                        _merge_original_and_canonical,
                        recipe_id,
                        idx,
                        original_text,
                        ci,
                    )
    finally:
        driver.close()

    print(f"Imported {len(data)} recipes from {input_path}")


if __name__ == "__main__":
    main()
