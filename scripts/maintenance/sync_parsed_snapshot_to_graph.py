#!/usr/bin/env python3
"""Replace the graph ingredient relationships of recipes with the corrected parsed snapshot.

Source of truth: ``weight_ready_parsed/`` (already excludes the recipes in ``excluded_recipes.json``).
Recipes are joined to the graph by URL (never by title). For every matched recipe this rewrites
``HAS_INGREDIENT`` (one relationship per parsed row, ``CREATE`` so repeated ingredients are kept) and the
``MAPS_TO`` edges of the recipe's ``Ingredients_original`` nodes, carrying the audited deterministic weight and its
provenance. Shared ``Ingredient`` nodes are reused by name (case-insensitive) so their allergen / FoodOn / cost links
survive; only genuinely new names create a node. ``Ingredients_original`` nodes are never modified.

``--delete-excluded`` removes the excluded recipes (graph node, Elasticsearch document, profile rows).

Dry-run by default. Take a three-store dump first (scripts/maintenance/dump_all_lite.py).

Usage:
  PYTHONPATH=src python scripts/maintenance/sync_parsed_snapshot_to_graph.py --files supervalu_v3_parsed.json          # dry run
  PYTHONPATH=src python scripts/maintenance/sync_parsed_snapshot_to_graph.py --files supervalu_v3_parsed.json --apply
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")
os.environ.setdefault("RECIPE1M_LLM_FALLBACK_ENABLED", "false")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from neo4j import GraphDatabase  # noqa: E402

from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda  # noqa: E402
from recipe_wrangler.tools.parse_recipe_tool import (  # noqa: E402
    _assign_lines_to_entries,
    split_salt_and_pepper_rows,
)
from scripts.prepare_weight_ready_parser_snapshot import RAW_SOURCE_PATHS, _title_key  # noqa: E402

SNAP = REPO / "data/processed/ingredient_parsing_final/2026-09-24"
PROJECTION_METHOD = "deterministic_weight_v3"
PROJECTION_VERSION = "parsed-snapshot-2026-09-26"
NEW_NODE_SOURCE = "parsed_snapshot_2026-09"

DATASETS = {
    "healthyfoods_final_parsed_fixed.json": ("HealthyFoods", "link", "title"),
    "myplate_final_parsed.json": ("MyPlate", "url", "title"),
    "foodhero_final_parsed.json": ("FoodHero", "source_url", "title"),
    "best_of_hungary_final_parsed.json": ("Best of Hungary", "url", "title"),
    "hungary_soul_final_parsed.json": ("The Hungary Soul", "url", "title"),
    "safefood_v1_parsed.json": ("Curated Irish Recipes", "url", "name"),
    "slovenian_v1_parsed.json": ("Slovenian Kitchen", "url", "title"),
    "supervalu_v3_parsed.json": ("SuperValu", "url", "title"),
    "irishheart_v3_parsed.json": ("Irish Heart Foundation", "url", "title"),
}


def _norm_url(url: object) -> str:
    return re.sub(r"[?#].*$", "", str(url or "")).rstrip("/").lower().replace("http://", "https://")


def _load_raw(filename: str, title_key: str) -> dict[str, dict]:
    raw: dict[str, dict] = {}
    for path in RAW_SOURCE_PATHS[filename]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for record in data if isinstance(data, list) else data.values():
            raw.setdefault(_title_key(record.get(title_key) or record.get("title") or record.get("name")), record)
    return raw


def _raw_positions(raw_lines: list[str]) -> tuple[list[str], list[int]]:
    """Adjacent duplicates are collapsed for line assignment; keep each kept line's original index."""
    lines, index = [], []
    for i, line in enumerate(raw_lines):
        text = str(line)
        if not lines or text != lines[-1]:
            lines.append(text)
            index.append(i)
    return lines, index


def build_rows(recipe: dict, raw_lines: list[str]) -> list[dict]:
    """Post-split rows (what the audit weighed) with weights, provenance and the raw-line position."""
    entries = recipe.get("ingredients") or []
    dedup_lines, dedup_index = _raw_positions(raw_lines)
    assigned = _assign_lines_to_entries(entries, dedup_lines)
    used: set[int] = set()
    line_position: list[int | None] = []
    for line in assigned:
        position = None
        if line is not None:
            for k, text in enumerate(dedup_lines):
                if text == line and dedup_index[k] not in used:
                    position = dedup_index[k]
                    used.add(position)
                    break
        line_position.append(position)

    names: list[str] = []
    measurements: list[str] = []
    origin: list[int] = []
    for i, entry in enumerate(entries):
        n, m, _, _ = split_salt_and_pepper_rows([entry["name"]], [entry["measurement"]])
        names += n
        measurements += m
        origin += [i] * len(n)
    result = ingredient_weight_tool_usda.invoke(
        {"ingredient_names": names, "measurements": measurements, "return_details": True, "debug": True}
    )
    weights, details = result["weights"], result["details"]
    next_free = len(raw_lines)
    rows = []
    for k, (name, measurement) in enumerate(zip(names, measurements)):
        entry = entries[origin[k]]
        position = line_position[origin[k]]
        if position is None:
            position, next_free = next_free, next_free + 1
        detail = details[k]
        rows.append({
            "name": name.strip(),
            "position": int(position),
            "props": {
                "measurement": str(measurement),
                "unit": detail.get("parsed_unit"),
                "quantity": _to_float(detail.get("parsed_quantity")),
                "weight_grams": float(weights[k]),
                "weight_match_type": detail.get("match_type"),
                "blank_quantity_policy": detail.get("blank_quantity_policy"),
                "display": str(entry.get("display") or ""),
                "note": str(entry.get("note") or ""),
                "projection_method": PROJECTION_METHOD,
                "projection_version": PROJECTION_VERSION,
            },
        })
    return rows


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _name_index(session) -> dict[str, list[str]]:
    index: dict[str, list[str]] = collections.defaultdict(list)
    for record in session.run("MATCH (i:Ingredient) RETURN i.name AS name"):
        if record["name"]:
            index[record["name"].casefold().strip()].append(record["name"])
    return index


def resolve_name(name: str, index: dict[str, list[str]]) -> str:
    """Reuse an existing shared node (exact, then the lowercase variant, then any); new names are lowercase."""
    variants = index.get(name.casefold().strip(), [])
    if name in variants:
        return name
    if name.casefold().strip() in variants:
        return name.casefold().strip()
    return variants[0] if variants else name.casefold().strip()


WRITE_ROWS = """
MATCH (r:Recipe {recipe_id: $rid})
WITH r
CALL { WITH r MATCH (r)-[old:HAS_INGREDIENT]->() DELETE old }
CALL { WITH r MATCH (o:Ingredients_original) WHERE o.original_id STARTS WITH (r.recipe_id + ':')
       MATCH (o)-[m:MAPS_TO]->() DELETE m }
WITH r
UNWIND $rows AS row
MERGE (c:Ingredient {name: row.name})
  ON CREATE SET c.canonical_id = randomUUID(), c.source = $node_source, c.status = 'resolved'
CREATE (r)-[h:HAS_INGREDIENT]->(c)
SET h += row.props, h.position = row.position
WITH r, row, c
OPTIONAL MATCH (o:Ingredients_original {original_id: r.recipe_id + ':' + toString(row.position)})
FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
  CREATE (o)-[m:MAPS_TO]->(c)
  SET m.measurement = row.props.measurement, m.unit = row.props.unit, m.weight_grams = row.props.weight_grams)
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="*", default=list(DATASETS), help="snapshot file names to sync")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-excluded", action="store_true")
    parser.add_argument("--report", type=Path, default=SNAP / "graph_sync_report.json")
    args = parser.parse_args()

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123")),
    )
    report = {"synced": [], "unmatched": [], "ambiguous": [], "excluded_deleted": []}
    with driver.session() as session:
        index = _name_index(session)
        for filename in args.files:
            source, url_key, title_key = DATASETS[filename]
            raw = _load_raw(filename, title_key)
            by_url: dict[str, list[str]] = collections.defaultdict(list)
            for record in session.run(
                "MATCH (r:Recipe {source: $s}) RETURN r.recipe_id AS id, r.url AS url", s=source
            ):
                by_url[_norm_url(record["url"])].append(record["id"])
            recipes = json.loads((SNAP / "weight_ready_parsed" / filename).read_text(encoding="utf-8"))
            for recipe in recipes[: args.limit or None]:
                record = raw.get(_title_key(recipe["title"]))
                ids = by_url.get(_norm_url((record or {}).get(url_key)), [])
                if len(ids) != 1:
                    (report["ambiguous"] if len(ids) > 1 else report["unmatched"]).append(
                        {"source": source, "title": recipe["title"]}
                    )
                    continue
                raw_lines = [str(line) for line in (record or {}).get("ingredients") or []]
                rows = build_rows(recipe, raw_lines)
                for row in rows:
                    row["name"] = resolve_name(row["name"], index)
                    index[row["name"].casefold().strip()] = index.get(row["name"].casefold().strip(), []) or [row["name"]]
                if args.apply:
                    session.execute_write(
                        lambda tx: tx.run(WRITE_ROWS, rid=ids[0], rows=rows, node_source=NEW_NODE_SOURCE).consume()
                    )
                report["synced"].append({"source": source, "recipe_id": ids[0], "title": recipe["title"], "rows": len(rows)})
            print(f"{source}: synced {sum(1 for s in report['synced'] if s['source'] == source)}, "
                  f"unmatched {sum(1 for s in report['unmatched'] if s['source'] == source)}", flush=True)

        if args.delete_excluded:
            excluded = json.loads((SNAP / "excluded_recipes.json").read_text(encoding="utf-8"))
            for item in excluded:
                source, url_key, title_key = DATASETS[item["source_file"]]
                record = _load_raw(item["source_file"], title_key).get(_title_key(item["recipe_title"]))
                ids = [
                    r["id"]
                    for r in session.run(
                        "MATCH (r:Recipe {source: $s}) WHERE toLower(coalesce(r.url,'')) STARTS WITH $u RETURN r.recipe_id AS id",
                        s=source, u=_norm_url((record or {}).get(url_key))[:200],
                    )
                ] if record else []
                report["excluded_deleted"].append({"source": source, "title": item["recipe_title"], "recipe_ids": ids})
    args.report.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print({k: len(v) for k, v in report.items()}, "applied" if args.apply else "dry-run")


if __name__ == "__main__":
    main()
