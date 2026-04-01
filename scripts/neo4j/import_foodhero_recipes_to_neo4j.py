#!/usr/bin/env python3
"""Import FoodHero recipes into Neo4j Recipe graph.

Uses deterministic 10-digit recipe_id generation to match the FoodHero Postgres
profiling import script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

try:
    from tqdm import tqdm
except Exception:
    tqdm = None


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "data" / "FoodHero" / "foodhero_recipes_clean.json"
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


def _as_text(value: object) -> str:
    return str(value or "").strip()


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


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
    # mixed numbers, e.g. "2 1/4" or "2 ¼"
    mixed = re.match(r"^\s*(\d+(?:\.\d+)?)\s+([0-9]+/[0-9]+|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])\b", s)
    if mixed:
        whole = float(mixed.group(1))
        frac = _parse_numeric_token(mixed.group(2)) or 0.0
        return whole + frac

    first = re.search(r"(\d+(?:\.\d+)?|[0-9]+/[0-9]+|[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])", s)
    if first:
        return _parse_numeric_token(first.group(1))
    return None


def _parse_serves(value: object) -> float | None:
    s = _as_text(value)
    if not s:
        return None
    return _parse_quantity(s)


def _parse_duration_minutes(value: object) -> float | None:
    s = _as_text(value).lower()
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
        unit = match.group(3)
        if unit.startswith("hour"):
            value_num *= 60.0
        total += value_num
        found = True

    if found:
        return total

    # fallback: treat first number as minutes if unit missing
    q = _parse_quantity(normalized)
    if q is not None:
        return q
    return 0.0


def _has_required_fields(recipe: dict[str, Any]) -> bool:
    return bool(_as_text(recipe.get("duration")) and _as_text(recipe.get("serves")))


def _recipe_seed(title_key: str, recipe: dict[str, Any]) -> str:
    return (
        _as_text(recipe.get("url"))
        or _as_text(recipe.get("title"))
        or _as_text(recipe.get("id"))
        or _as_text(recipe.get("recipe_id"))
        or _as_text(title_key)
        or "foodhero_recipe"
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


def _ensure_constraints(session) -> None:
    session.run(
        "CREATE CONSTRAINT recipe_recipe_id IF NOT EXISTS "
        "FOR (r:Recipe) REQUIRE r.recipe_id IS UNIQUE"
    )
    session.run(
        "CREATE CONSTRAINT ingredients_original_id IF NOT EXISTS "
        "FOR (o:Ingredients_original) REQUIRE o.original_id IS UNIQUE"
    )
    session.run(
        "CREATE INDEX ingredient_name_idx IF NOT EXISTS "
        "FOR (i:Ingredient) ON (i.name)"
    )


def _merge_recipe(tx, recipe_id: str, recipe: dict[str, Any], title_key: str) -> None:
    q = """
    MERGE (r:Recipe {recipe_id: $recipe_id})
    SET r.title = $title,
        r.url = $url,
        r.image_url = $image_url,
        r.instructions = $instructions,
        r.source = 'FoodHero',
        r.status = 'active',
        r.duration = $duration,
        r.serves = $serves
    """
    duration = _parse_duration_minutes(recipe.get("duration"))
    serves = _parse_serves(recipe.get("serves"))
    tx.run(
        q,
        {
            "recipe_id": recipe_id,
            "title": _as_text(recipe.get("title")) or title_key,
            "url": _as_text(recipe.get("url")) or None,
            "image_url": _as_text(recipe.get("image_url")) or None,
            "instructions": _as_list(recipe.get("instructions")),
            "duration": duration if duration is not None else 0.0,
            "serves": serves if serves is not None else 0.0,
        },
    )


def _merge_ingredient(tx, recipe_id: str, position: int, ingredient_text: str) -> None:
    ingredient_name = _as_text(ingredient_text)
    q = """
    MATCH (r:Recipe {recipe_id: $recipe_id})
    MERGE (o:Ingredients_original {original_id: $original_id})
    SET o.name = $ingredient_name,
        o.original_text = $ingredient_text,
        o.source = 'FoodHero',
        o.status = 'active'
    MERGE (r)-[hio:HAS_INGREDIENT_ORIGINAL {position: $position}]->(o)

    MERGE (i:Ingredient {name: $ingredient_name})
    ON CREATE SET
        i.canonical_id = randomUUID(),
        i.source = 'FoodHero',
        i.status = 'resolved'
    ON MATCH SET
        i.canonical_id = coalesce(i.canonical_id, randomUUID()),
        i.source = coalesce(i.source, 'Recipe1M'),
        i.status = coalesce(i.status, 'resolved')

    MERGE (o)-[:MAPS_TO]->(i)
    MERGE (r)-[hi:HAS_INGREDIENT]->(i)
    ON CREATE SET
        hi.measurement = null,
        hi.unit = null
    """
    tx.run(
        q,
        {
            "recipe_id": recipe_id,
            "original_id": f"{recipe_id}:{position}",
            "position": position,
            "ingredient_name": ingredient_name,
            "ingredient_text": ingredient_name,
        },
    )


def _iter_items(items: list[tuple[str, dict[str, Any]]]):
    if tqdm is not None:
        yield from tqdm(items, total=len(items), desc="Import FoodHero", unit="recipe")
        return
    total = len(items)
    for idx, item in enumerate(items, start=1):
        print(f"[{idx}/{total}] importing")
        yield item


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="FoodHero clean JSON path")
    parser.add_argument(
        "--keep-missing-required",
        action="store_true",
        help="Keep recipes missing duration/serves (default is to drop them).",
    )
    args = parser.parse_args()

    input_path = args.input if args.input.is_absolute() else (REPO_ROOT / args.input)
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Input JSON must be an object keyed by recipe title.")

    dropped = 0
    prepared: list[tuple[str, dict[str, Any]]] = []
    for title_key, payload in raw.items():
        if not isinstance(payload, dict):
            dropped += 1
            continue
        if (not args.keep_missing_required) and (not _has_required_fields(payload)):
            dropped += 1
            continue
        recipe = dict(payload)
        recipe.pop("notes", None)
        prepared.append((str(title_key), recipe))

    used_ids: set[str] = set()
    driver = _driver()
    imported = 0
    try:
        with driver.session() as session:
            _ensure_constraints(session)
            for title_key, recipe in _iter_items(prepared):
                recipe_id = _next_unique_id(_recipe_seed(title_key, recipe), used_ids)
                session.execute_write(_merge_recipe, recipe_id, recipe, title_key)
                for pos, ingredient_text in enumerate(_as_list(recipe.get("ingredients"))):
                    session.execute_write(_merge_ingredient, recipe_id, pos, ingredient_text)
                imported += 1
    finally:
        driver.close()

    print(
        json.dumps(
            {
                "input_rows": len(raw),
                "dropped_missing_duration_or_serves": dropped,
                "imported_rows": imported,
                "source": "FoodHero",
                "input_path": str(input_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
