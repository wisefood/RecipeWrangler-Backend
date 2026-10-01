#!/usr/bin/env python3
"""Normalize MyPlate recipe ids to unique 10-digit strings and migrate JSON + Neo4j."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase


REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data" / "MyPlate"

DEFAULT_CANONICAL = DATA_DIR / "myplate_recipes_with_canonical_ingredients.json"
DEFAULT_MAP = DATA_DIR / "myplate_recipe_id_map.json"
DEFAULT_FILES = [
    DATA_DIR / "myplate_recipes.json",
    DATA_DIR / "myplate_recipes_clean.json",
    DATA_DIR / "myplate_recipes_with_canonical_ingredients.json",
    DATA_DIR / "myplate_recipes_nutrition_usda_mweight.json",
    DATA_DIR / "myplate_recipes_nutrition_usda.json",
]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _norm_key(value: Any) -> str | None:
    text = _clean(value)
    return text.lower() if text else None


def _seed_for_recipe(title_key: str, recipe: dict[str, Any]) -> str:
    return (
        _norm_key(recipe.get("url"))
        or _norm_key(recipe.get("title"))
        or _norm_key(recipe.get("id"))
        or _norm_key(recipe.get("recipe_id"))
        or _norm_key(title_key)
        or "myplate_recipe"
    )


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


def _driver():
    load_dotenv(REPO_ROOT / ".env")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j"))
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError("Missing NEO4J_PASSWORD (or set NEO4J_NO_AUTH=1).")
    return GraphDatabase.driver(uri, auth=(username, password))


def _existing_non_myplate_ids() -> set[str]:
    used: set[str] = set()
    try:
        driver = _driver()
    except Exception:
        return used
    try:
        with driver.session() as session:
            rows = session.run(
                """
                MATCH (r:Recipe)
                WHERE coalesce(r.source, '') <> 'MyPlate'
                RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS rid
                """
            )
            for row in rows:
                rid = _clean(row.get("rid"))
                if rid:
                    used.add(rid)
    finally:
        driver.close()
    return used


def _build_mapping(canonical_path: Path, reserve_non_myplate: bool) -> list[dict[str, Any]]:
    data = json.loads(canonical_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Canonical MyPlate file must be an object keyed by title.")

    used_ids = _existing_non_myplate_ids() if reserve_non_myplate else set()
    mapping: list[dict[str, Any]] = []
    for title_key, payload in data.items():
        if not isinstance(payload, dict):
            continue
        seed = _seed_for_recipe(str(title_key), payload)
        recipe_id = _next_unique_id(seed, used_ids)
        old_ids = []
        for value in [payload.get("recipe_id"), payload.get("id")]:
            cleaned = _clean(value)
            if cleaned and cleaned not in old_ids:
                old_ids.append(cleaned)
        mapping.append(
            {
                "title_key": str(title_key),
                "title": _clean(payload.get("title")) or str(title_key),
                "url": _clean(payload.get("url")),
                "old_ids": old_ids,
                "recipe_id": recipe_id,
            }
        )
    return mapping


def _build_lookup(mapping: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in mapping:
        rid = row["recipe_id"]
        for value in [row.get("title_key"), row.get("title"), row.get("url"), *row.get("old_ids", [])]:
            key = _norm_key(value)
            if key:
                lookup[key] = rid
    return lookup


def _rewrite_json(path: Path, lookup: dict[str, str]) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    updated = 0
    total = 0

    if isinstance(data, dict):
        for title_key, payload in data.items():
            if not isinstance(payload, dict):
                continue
            total += 1
            rid = None
            for value in [
                payload.get("recipe_id"),
                payload.get("id"),
                payload.get("url"),
                payload.get("title"),
                title_key,
            ]:
                key = _norm_key(value)
                if key and key in lookup:
                    rid = lookup[key]
                    break
            if not rid:
                continue
            if payload.get("recipe_id") != rid or payload.get("id") != rid:
                payload["recipe_id"] = rid
                payload["id"] = rid
                updated += 1
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            total += 1
            rid = None
            for value in [
                row.get("recipe_id"),
                row.get("id"),
                row.get("url"),
                row.get("title"),
            ]:
                key = _norm_key(value)
                if key and key in lookup:
                    rid = lookup[key]
                    break
            if not rid:
                continue
            if row.get("recipe_id") != rid or row.get("id") != rid:
                row["recipe_id"] = rid
                row["id"] = rid
                updated += 1
    else:
        raise ValueError(f"Unsupported JSON shape in {path}")

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return updated, total


def _migrate_neo(mapping: list[dict[str, Any]]) -> tuple[int, int]:
    driver = _driver()
    updated_recipes = 0
    updated_originals = 0
    try:
        with driver.session() as session:
            for row in mapping:
                recipe_id = row["recipe_id"]
                old_ids = [x for x in row.get("old_ids", []) if x]
                title = row.get("title")
                url = row.get("url")

                result = session.run(
                    """
                    MATCH (r:Recipe)
                    WHERE coalesce(r.source, '') = 'MyPlate'
                      AND (
                        size($old_ids) > 0 AND (r.recipe_id IN $old_ids OR r.id IN $old_ids)
                        OR ($url IS NOT NULL AND r.url = $url)
                        OR ($title IS NOT NULL AND r.title = $title)
                      )
                    SET r.recipe_id = $recipe_id,
                        r.id = $recipe_id
                    RETURN count(r) AS c
                    """,
                    {
                        "recipe_id": recipe_id,
                        "old_ids": old_ids,
                        "title": title,
                        "url": url,
                    },
                ).single()
                updated_recipes += int(result["c"] if result and result["c"] is not None else 0)

                result2 = session.run(
                    """
                    MATCH (r:Recipe {recipe_id: $recipe_id})-[h:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original)
                    WITH o, h, $recipe_id AS rid, coalesce(h.position, 0) AS pos
                    SET o.original_id = rid + ':' + toString(pos)
                    RETURN count(o) AS c
                    """,
                    {"recipe_id": recipe_id},
                ).single()
                updated_originals += int(result2["c"] if result2 and result2["c"] is not None else 0)
    finally:
        driver.close()
    return updated_recipes, updated_originals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize MyPlate recipe ids to unique 10-digit recipe_id values."
    )
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--map-out", type=Path, default=DEFAULT_MAP)
    parser.add_argument(
        "--files",
        nargs="*",
        default=[str(p) for p in DEFAULT_FILES],
        help="JSON files to rewrite in-place.",
    )
    parser.add_argument(
        "--no-neo",
        action="store_true",
        help="Skip Neo4j migration.",
    )
    parser.add_argument(
        "--no-neo-reserve",
        action="store_true",
        help="Do not reserve non-MyPlate recipe ids from Neo4j when generating IDs.",
    )
    args = parser.parse_args()

    mapping = _build_mapping(
        canonical_path=args.canonical,
        reserve_non_myplate=not args.no_neo_reserve,
    )
    args.map_out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lookup = _build_lookup(mapping)

    for file_arg in args.files:
        path = Path(file_arg)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            print(f"skip missing: {path}")
            continue
        try:
            updated, total = _rewrite_json(path, lookup)
            print(f"rewrote {path} | updated={updated}/{total}")
        except Exception as exc:  # noqa: BLE001
            print(f"skip invalid json: {path} | {exc}")

    if not args.no_neo:
        recipe_count, original_count = _migrate_neo(mapping)
        print(
            f"neo migrated | recipes_touched={recipe_count} "
            f"ingredients_original_touched={original_count}"
        )

    print(f"mapping_rows={len(mapping)} map_file={args.map_out}")


if __name__ == "__main__":
    main()
