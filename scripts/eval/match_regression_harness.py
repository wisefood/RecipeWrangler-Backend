#!/usr/bin/env python3
"""Regression harness for nutrition_match.py changes.

Dumps every distinct (recovered) ingredient match-name currently in Postgres,
re-runs best_nutrition_match on each, and writes name -> (canonical_food_id,
match_confidence, reason). Run once before a matcher change (baseline) and
once after (current), then diff the two files -- anything that changed
canonical_food_id that wasn't the intended fix is a regression.

Usage:
  PYTHONPATH=src .venv/bin/python scripts/eval/match_regression_harness.py --out baseline.csv
  ... make matcher changes ...
  PYTHONPATH=src .venv/bin/python scripts/eval/match_regression_harness.py --out after.csv
  python -c "diff logic between the two CSVs"
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from recipe_wrangler.tools.nutrition_match import best_nutrition_match

REGIONS = ["eu", "irish", "hungarian", "slovenian"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("NUTRITION_HOST", "localhost"), port=os.getenv("NUTRITION_PORT", "5432"),
        dbname=os.getenv("NUTRITION_DB", "nutrients"), user=os.getenv("NUTRITION_USER", "postgres"),
        password=os.getenv("NUTRITION_PASSWORD", "postgres"),
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT elem->>'ingredient' AS name
        FROM "nutrients-recipe-profiles" p,
        LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(p.nutrition_profiling_details)='array'
                 THEN p.nutrition_profiling_details ELSE '[]'::jsonb END) elem
        WHERE p.nutrition_source = 'eu' AND elem->>'ingredient' IS NOT NULL
    """)
    names = sorted({r["name"] for r in cur.fetchall() if r["name"]})
    if args.limit:
        names = names[: args.limit]
    print(f"{len(names)} distinct ingredient names to re-match across {len(REGIONS)} regions")

    rows = []
    for i, name in enumerate(names):
        for region in REGIONS:
            try:
                result = best_nutrition_match(name, source=region)
            except Exception as exc:
                rows.append({"name": name, "region": region, "canonical_food_id": "ERROR",
                             "confidence": str(exc)[:100], "reason": "exception"})
                continue
            match = result.get("match") or {}
            food_id = match.get("id") or (match.get("metadata") or {}).get("eu_id") or ""
            rows.append({
                "name": name, "region": region, "canonical_food_id": food_id,
                "confidence": result.get("confidence"), "reason": result.get("reason"),
            })
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(names)}", flush=True)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "region", "canonical_food_id", "confidence", "reason"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
