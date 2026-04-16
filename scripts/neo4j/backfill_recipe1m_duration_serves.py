#!/usr/bin/env python3
"""Backfill duration and serves on recipe1m Neo4j nodes using HUMMUS data.

Pipeline:
1) Load data/raw/hummus/recipes.csv  → {food_com_id: (duration_mins, serves)}
2) Load data/raw/recipe1m/layer1.json → {hex_id: food_com_id}  (via food.com URL suffix)
3) Join and batch-update Neo4j r.duration, r.serves
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import run_query

HUMMUS_CSV   = REPO_ROOT / "data" / "raw" / "hummus" / "recipes.csv"
LAYER1_JSON  = REPO_ROOT / "data" / "raw" / "recipe1m" / "layer1.json"
BATCH_SIZE   = 500


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_duration(raw: str | None) -> float | None:
    """Convert HUMMUS duration string to total minutes as float."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    hrs  = re.search(r"(\d+(?:\.\d+)?)\s*hr", s)
    mins = re.search(r"(\d+(?:\.\d+)?)\s*min", s)
    total = 0.0
    if hrs:
        total += float(hrs.group(1)) * 60
    if mins:
        total += float(mins.group(1))
    return total if total > 0 else None


def _parse_serves(raw: str | None) -> float | None:
    """Convert HUMMUS serves string to float (midpoint for ranges)."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    # Range like "2-4" or "10-12"
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$", s)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2
    # Plain number
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Build lookup
# ---------------------------------------------------------------------------

def build_lookup() -> dict[str, dict[str, float]]:
    """Return {recipe1m_hex_id: {duration: float, serves: float}}."""
    print("Loading HUMMUS recipes.csv...")
    df = pd.read_csv(HUMMUS_CSV, usecols=["recipe_id", "duration", "serves"])
    hummus: dict[str, tuple] = {}
    for _, row in df.iterrows():
        fc_id = str(int(row["recipe_id"]))
        dur   = _parse_duration(row["duration"] if pd.notna(row["duration"]) else None)
        srv   = _parse_serves(row["serves"] if pd.notna(row["serves"]) else None)
        hummus[fc_id] = (dur, srv)
    print(f"  {len(hummus)} HUMMUS entries loaded")

    print("Loading recipe1m layer1.json (this takes ~10s)...")
    with open(LAYER1_JSON, encoding="utf-8") as f:
        layer1 = json.load(f)

    foodcom_re = re.compile(r"-(\d+)$")
    lookup: dict[str, dict[str, float]] = {}
    for entry in layer1:
        url = entry.get("url", "")
        if "food.com" not in url:
            continue
        m = foodcom_re.search(url)
        if not m:
            continue
        fc_id   = m.group(1)
        hex_id  = entry["id"]
        row_val = hummus.get(fc_id)
        if not row_val:
            continue
        dur, srv = row_val
        record: dict[str, float] = {}
        if dur is not None:
            record["duration"] = dur
        if srv is not None:
            record["serves"] = srv
        if record:
            lookup[hex_id] = record

    print(f"  {len(lookup)} recipe1m entries matched")
    return lookup


# ---------------------------------------------------------------------------
# Neo4j update
# ---------------------------------------------------------------------------

def run_backfill(lookup: dict[str, dict[str, float]], dry_run: bool) -> dict[str, Any]:
    ids      = list(lookup.keys())
    total    = len(ids)
    updated  = 0
    skipped  = 0

    batches = [ids[i : i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    bar = tqdm(batches, desc="Updating Neo4j", unit="batch")

    for batch_ids in bar:
        updates = [
            {
                "id": hex_id,
                "duration": lookup[hex_id].get("duration"),
                "serves":   lookup[hex_id].get("serves"),
            }
            for hex_id in batch_ids
        ]

        if not dry_run:
            result = run_query(
                """
                UNWIND $updates AS u
                MATCH (r:Recipe {recipe_id: u.id})
                SET r.duration = CASE WHEN u.duration IS NOT NULL THEN u.duration ELSE r.duration END,
                    r.serves   = CASE WHEN u.serves   IS NOT NULL THEN u.serves   ELSE r.serves   END
                RETURN count(r) AS n
                """,
                {"updates": updates},
            )
            updated += result[0]["n"]
        else:
            skipped += len(batch_ids)

        bar.set_postfix(updated=updated, skipped=skipped)

    return {
        "total_matched": total,
        "updated": updated,
        "skipped_dry_run": skipped,
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Apply updates to Neo4j (default: dry-run)")
    args = parser.parse_args()

    lookup = build_lookup()
    result = run_backfill(lookup, dry_run=not args.write)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
