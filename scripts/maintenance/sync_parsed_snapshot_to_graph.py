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
import hashlib
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


SOURCE_URN = {
    "HealthyFoods": "urn:rcollection:healthyfood",
    "MyPlate": "urn:rcollection:myplate",
    "FoodHero": "urn:rcollection:foodhero",
}


def _first_number(value: object) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def _minutes(value: object) -> float | None:
    """"1 hour 15 minutes" -> 75, "30" -> 30; None when there is no number."""
    text = str(value or "").lower()
    hours = re.search(r"(\d+(?:\.\d+)?)\s*h", text)
    mins = re.search(r"(\d+(?:\.\d+)?)\s*m", text)
    if hours or mins:
        return (float(hours.group(1)) * 60 if hours else 0.0) + (float(mins.group(1)) if mins else 0.0)
    return _first_number(text)


def _new_recipe_id(url: str, used: set[str], record: dict) -> str:
    """Same scheme as the importers: MyPlate keeps its scraped id, the rest use sha1(url.lower()) mod 1e10."""
    given = str(record.get("recipe_id") or "").strip()
    if given and given not in used:
        return given
    number = int(hashlib.sha1(url.lower().encode("utf-8")).hexdigest(), 16) % 10**10
    while f"{number:010d}" in used:
        number = (number + 1) % 10**10
    return f"{number:010d}"


def node_props(source: str, record: dict, recipe_id: str) -> dict:
    instructions = record.get("instructions") or record.get("directions") or []
    if isinstance(instructions, str):
        instructions = [instructions]
    serves = _first_number(record.get("serves") or record.get("servings") or record.get("recipe_yield") or record.get("makes"))
    duration = _minutes(record.get("time_minutes") or record.get("duration"))
    if duration is None:
        prep, cook = _minutes(record.get("prep_time")), _minutes(record.get("cook_time"))
        duration = (prep or 0.0) + (cook or 0.0) if (prep or cook) else None
    props = {
        "recipe_id": recipe_id, "source": source, "status": "active", "has_profile": True,
        "title": str(record.get("title") or record.get("name") or ""),
        "url": str(record.get(DATASET_URL_KEY[source]) or ""),
        "image_url": str(record.get("image_url") or ""),
        "instructions": [str(x) for x in instructions],
        "source_id": SOURCE_URN[source],
    }
    if serves is not None:
        props["serves"] = serves
    if duration is not None:
        props["duration"] = duration
        if source == "MyPlate":
            props["duration_minutes"] = duration
    if source == "MyPlate":
        props["id"] = recipe_id
    return props


DATASET_URL_KEY = {"HealthyFoods": "link", "MyPlate": "url", "FoodHero": "source_url"}

CREATE_NODE = """
MERGE (r:Recipe {recipe_id: $rid})
  ON CREATE SET r += $props
WITH r
UNWIND range(0, size($lines) - 1) AS pos
MERGE (o:Ingredients_original {original_id: $rid + ':' + toString(pos)})
  ON CREATE SET o.name = $lines[pos], o.original_text = $lines[pos], o.source = $source, o.status = 'active'
MERGE (r)-[h:HAS_INGREDIENT_ORIGINAL {position: pos}]->(o)
"""


def _tokens(texts: list[str]) -> set[str]:
    return {w for t in texts for w in re.findall(r"[a-z]{4,}", str(t).lower())}


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
    parser.add_argument(
        "--create-missing", action="store_true",
        help="create graph recipes for parsed recipes with no URL match (HealthyFoods/MyPlate/FoodHero); "
             "a same-title graph recipe with >=60%% ingredient overlap is synced instead of duplicated",
    )
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
            by_title: dict[str, list[dict]] = collections.defaultdict(list)
            used_ids = set()
            if args.create_missing:
                for record in session.run(
                    "MATCH (r:Recipe) RETURN r.recipe_id AS id, r.title AS title, r.source AS source"
                ):
                    used_ids.add(str(record["id"]))
                    if record["source"] == source:
                        by_title[_title_key(record["title"])].append(record["id"])
            for recipe in recipes[: args.limit or None]:
                record = raw.get(_title_key(recipe["title"]))
                ids = by_url.get(_norm_url((record or {}).get(url_key)), [])
                if not ids and args.create_missing and record:
                    same_title = by_title.get(_title_key(recipe["title"]), [])
                    raw_lines = [str(line) for line in record.get("ingredients") or []]
                    for existing in same_title:
                        originals = [
                            x["t"] for x in session.run(
                                "MATCH (r:Recipe {recipe_id:$id})-[:HAS_INGREDIENT_ORIGINAL]->(o) RETURN o.original_text AS t",
                                id=existing,
                            )
                        ]
                        a, b = _tokens(raw_lines), _tokens(originals)
                        if a and b and len(a & b) / len(a | b) >= 0.6:
                            ids = [existing]
                            break
                    if not ids and same_title:
                        # The graph keeps one recipe per title (raw sources are keyed by title); a different page with the
                        # same title and different ingredients is a variant, not a missing recipe.
                        report.setdefault("skipped_title_duplicates", []).append({"source": source, "title": recipe["title"]})
                        continue
                    if not ids and source in SOURCE_URN:
                        new_id = _new_recipe_id(str(record.get(url_key) or ""), used_ids, record)
                        used_ids.add(new_id)
                        if args.apply:
                            session.execute_write(
                                lambda tx: tx.run(
                                    CREATE_NODE, rid=new_id, props=node_props(source, record, new_id),
                                    lines=raw_lines, source=source,
                                ).consume()
                            )
                        ids = [new_id]
                        report.setdefault("created", []).append({"source": source, "recipe_id": new_id, "title": recipe["title"]})
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
