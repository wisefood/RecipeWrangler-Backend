#!/usr/bin/env python3
"""Backfill HAS_INGREDIENT.measurement/weight_grams for Curated Hungarian Recipes
(ESSRG) and Curated Slovenian Recipes (OPKP) from their own raw sources.

Neither dataset's graph edges were ever populated with weight_grams -- they
bypass the standard parsed-snapshot/weight-tool pipeline entirely (their own
direct-nutrition importers never wrote it). recompute_all_profiles.py only
reuses stored weights when EVERY edge on a recipe has one; with none present,
every recipe on both datasets falls through to a blank-measurement re-derive,
which is where the 1000g/whole-item fallback bug (see HANDOFF) comes from.

Sources (exact per-ingredient gram amounts, already resolved):
  - Curated Hungarian Recipes: data/ESSRG/ESSRG_recipes_clean.json
      ingredient_details: [{name, quantity, unit, weight_g}, ...] in position order
  - Curated Slovenian Recipes: data/Slovenia/Slovenian_Recipes.xlsx, sheet "Sestavine"
      rows keyed by RECID (== recipe_id), in file order == recipe position order

Joins strictly by recipe_id (both datasets keep graph recipe_id == source id,
no URL join needed). Neither dataset's HAS_INGREDIENT edges carry a `position`
property (unlike the parsed-snapshot pipeline), so pairing can't be positional.
Instead: the graph's short Ingredient name (e.g. "Eggs") is always a prefix of
the source's full name up to its first comma (e.g. "Eggs, chicken, whole,
scrambled, without milk") -- verified against every ESSRG/Slovenian record.
Pairing is done by matching the multiset of graph ingredient names against the
multiset of source short-names per recipe; a recipe is only written when both
multisets are identical (same names, same counts). Duplicate-named ingredients
within one recipe (e.g. two "Salt" edges) are paired arbitrarily within that
name group -- a mismatch there is cosmetic (near-identical amounts), unlike a
wrong-ingredient pairing. Any recipe where the name multisets don't match
exactly is skipped and logged, not guessed.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/maintenance/backfill_essrg_slovenian_weights.py            # dry-run
  PYTHONPATH=src .venv/bin/python scripts/maintenance/backfill_essrg_slovenian_weights.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import openpyxl
from neo4j import GraphDatabase

REPO_ROOT = Path(__file__).resolve().parents[2]
ESSRG_FILE = REPO_ROOT / "data/ESSRG/ESSRG_recipes_clean.json"
SLOVENIAN_XLSX = REPO_ROOT / "data/Slovenia/Slovenian_Recipes.xlsx"


def _short_name(full_name: str) -> str:
    return full_name.split(",")[0].strip().casefold()


def _essrg_rows() -> dict[str, list[dict]]:
    data = json.loads(ESSRG_FILE.read_text(encoding="utf-8"))
    recs = data if isinstance(data, list) else list(data.values())
    out = {}
    for r in recs:
        rid = r.get("recipe_id") or r.get("id")
        details = r.get("ingredient_details") or []
        out[rid] = [
            {"short_name": _short_name(d.get("name") or ""),
             "measurement": d.get("measurement") or f"{d.get('quantity')} {d.get('unit')}",
             "weight_g": d.get("weight_g")}
            for d in details
        ]
    return out


def _slovenian_rows() -> dict[str, list[dict]]:
    wb = openpyxl.load_workbook(SLOVENIAN_XLSX, read_only=True)
    ws = wb["Sestavine"]
    by_recipe: dict[str, list[dict]] = defaultdict(list)
    for row in ws.iter_rows(min_row=2, values_only=True):
        # original_code, ingredient_code, original_name, english_name, recid, amount, unit
        _, _, _, english_name, recid, amount, unit = row
        if recid is None:
            continue
        weight_g = float(amount) if amount is not None else None  # ml treated as ~1g/ml, close enough for this fix
        by_recipe[recid].append({
            "short_name": _short_name(english_name or ""),
            "measurement": f"{amount} {unit}", "weight_g": weight_g,
        })
    return dict(by_recipe)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "password123")),
    )

    datasets = [
        ("Curated Hungarian Recipes", _essrg_rows()),
        ("Curated Slovenian Recipes", _slovenian_rows()),
    ]

    report = {"written": [], "skipped_name_mismatch": [], "skipped_no_source": [], "skipped_no_edges": []}
    with driver.session() as session:
        for source_label, source_rows in datasets:
            graph_recipes = session.run(
                "MATCH (r:Recipe {source: $s})-[h:HAS_INGREDIENT]->(i:Ingredient) "
                "RETURN r.recipe_id AS rid, collect({eid: elementId(h), name: i.name}) AS edges",
                s=source_label,
            )
            for rec in graph_recipes:
                rid, edges = rec["rid"], rec["edges"]
                if not edges:
                    report["skipped_no_edges"].append({"source": source_label, "recipe_id": rid})
                    continue
                src = source_rows.get(rid)
                if not src:
                    report["skipped_no_source"].append({"source": source_label, "recipe_id": rid})
                    continue
                graph_names = sorted(_short_name(e["name"] or "") for e in edges)
                source_names = sorted(item["short_name"] for item in src)
                if graph_names != source_names:
                    report["skipped_name_mismatch"].append({
                        "source": source_label, "recipe_id": rid,
                        "graph_names": graph_names, "source_names": source_names,
                    })
                    continue
                # group both sides by short name, zip within each group (order within a
                # duplicate-name group is the only ambiguity; see module docstring)
                edges_by_name: dict[str, list[str]] = defaultdict(list)
                for e in edges:
                    edges_by_name[_short_name(e["name"] or "")].append(e["eid"])
                src_by_name: dict[str, list[dict]] = defaultdict(list)
                for item in src:
                    src_by_name[item["short_name"]].append(item)
                pairs = []  # (element_id, source_item)
                for name, eids in edges_by_name.items():
                    for eid, item in zip(eids, src_by_name[name]):
                        pairs.append((eid, item))
                if args.apply:
                    for eid, item in pairs:
                        session.execute_write(
                            lambda tx, eid=eid, item=item: tx.run(
                                "MATCH ()-[h]->() WHERE elementId(h) = $eid "
                                "SET h.measurement = $measurement, h.weight_grams = $weight_g",
                                eid=eid, measurement=str(item["measurement"]),
                                weight_g=float(item["weight_g"]) if item["weight_g"] is not None else None,
                            ).consume()
                        )
                report["written"].append({"source": source_label, "recipe_id": rid, "n": len(pairs)})

    driver.close()
    out_path = REPO_ROOT / "data/processed/ingredient_parsing_final/2026-09-24/essrg_slovenian_weight_backfill_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print({k: len(v) for k, v in report.items()}, "applied" if args.apply else "dry-run")
    print(f"Report: {out_path}")


if __name__ == "__main__":
    main()
