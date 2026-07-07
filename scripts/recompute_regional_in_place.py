#!/usr/bin/env python3
"""Recompute IE or HU recipe profiles in place, reusing prior parse + weights.

For every recipe that already has a row in the target region (IE or HU),
take ``nutrition_profiling_details`` (name + measurement + weight_g — all
region-independent) from the most-recent existing profile row and re-run
only the nutrition + sustainability + nutri-score stages for the target
region under the updated matcher rules (no USDA short-circuits; falls back
to EU instead).

Skips Recipe_Parser and Weight_Calculator entirely — no vLLM needed.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/recompute_regional_in_place.py --region IE --limit 2
    PYTHONPATH=src .venv/bin/python scripts/recompute_regional_in_place.py --region IE --write
    PYTHONPATH=src .venv/bin/python scripts/recompute_regional_in_place.py --region HU --write
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.utils.nutrition_postgres import (  # noqa: E402
    get_connection,
    upsert_recipe_profiling_trace,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from recompute_all_profiles import (  # noqa: E402
    _build_record,
    _profile_one,
)

# region -> (LangGraph region arg, postgres nutrition_source value)
_REGION_MAP = {
    "IE": ("IE", "irish"),
    "HU": ("HU", "hungarian"),
}

OUT_DIR = REPO_ROOT / "data_to_send"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    _stop = True
    print("\n[regional-reuse] stop requested — finishing current recipe then exiting.",
          flush=True)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _ckpt_path(region: str) -> Path:
    return OUT_DIR / f"recompute_regional_in_place_{region.lower()}.checkpoint.json"


def _fail_path(region: str) -> Path:
    return OUT_DIR / f"recompute_regional_in_place_{region.lower()}.failures.jsonl"


def load_checkpoint(region: str) -> set[str]:
    p = _ckpt_path(region)
    if p.exists():
        with open(p) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(region: str, done: set[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = _ckpt_path(region)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(done), f)
    tmp.replace(p)


def append_failure(region: str, rec: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_fail_path(region), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_seed_rows(region_pg: str, limit: int | None) -> list[dict]:
    """One row per recipe_id that already has a profile in the target region.

    Picks the most-recent row across all regions as the parse+weight seed —
    name/measurement/weight_g are region-independent.
    """
    lim = f"LIMIT {int(limit)}" if limit else ""
    q = text(
        f"""
        SELECT DISTINCT ON (p.recipe_id)
            p.recipe_id,
            p.title,
            p.source,
            p.nutrition_source AS seed_source,
            p.nutrition_profiling_details,
            p.trace
        FROM "nutrients-recipe-profiles" p
        WHERE EXISTS (
            SELECT 1 FROM "nutrients-recipe-profiles" q
            WHERE q.recipe_id = p.recipe_id AND q.nutrition_source = :region
        )
        ORDER BY p.recipe_id, p.computed_at DESC
        {lim}
        """
    )
    with get_connection() as conn:
        rows = conn.execute(q, {"region": region_pg}).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "recipe_id": r[0],
            "title": r[1] or "Untitled Recipe",
            "source_label": r[2] or "unknown",
            "seed_source": r[3],
            "details": r[4] or [],
            "trace": r[5] or {},
        })
    return out


def build_rec_from_seed(seed: dict) -> dict | None:
    details = seed.get("details") or []
    names: list[str] = []
    measurements: list[str] = []
    weights: list[float] = []
    for d in details:
        nm = (d.get("name") or d.get("ingredient") or "").strip()
        if not nm:
            continue
        names.append(nm)
        measurements.append(str(d.get("measurement") or ""))
        try:
            w = float(d.get("weight_g") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        weights.append(w)
    if not names:
        return None
    serves = None
    tr = seed.get("trace") or {}
    if isinstance(tr, dict):
        try:
            sv = tr.get("serves")
            serves = float(sv) if sv is not None else None
        except (TypeError, ValueError):
            serves = None
    return {
        "recipe_id": seed["recipe_id"],
        "title": seed["title"],
        "ingredient_names": names,
        "measurements": measurements,
        "weights": weights,
        "instructions": [],
        "serves": serves,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, choices=sorted(_REGION_MAP.keys()),
                    help="target region (IE or HU)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap seed rows fetched (handy for smoke tests)")
    ap.add_argument("--write", action="store_true", help="actually UPSERT to Postgres")
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore checkpoint and re-process everything")
    ap.add_argument("--checkpoint-every", type=int, default=25,
                    help="flush checkpoint every N successful recipes")
    args = ap.parse_args()

    region_lg, region_pg = _REGION_MAP[args.region]

    seeds = fetch_seed_rows(region_pg, args.limit)
    print(f"[regional-reuse] region={args.region} found {len(seeds)} candidate recipes "
          f"(have an existing {region_pg} row).", flush=True)
    if not seeds:
        return

    done = set() if args.no_resume else load_checkpoint(args.region)
    if done:
        print(f"[regional-reuse] checkpoint: {len(done)} already done, will skip.", flush=True)

    n_ok = n_fail = n_skip = 0
    t0 = time.time()
    pending_ckpt = 0

    for i, seed in enumerate(seeds, 1):
        if _stop:
            break
        rid = seed["recipe_id"]
        key = f"{rid}|{args.region}"
        if key in done:
            n_skip += 1
            continue

        rec = build_rec_from_seed(seed)
        if rec is None:
            n_fail += 1
            append_failure(args.region, {"recipe_id": rid, "reason": "no ingredients in seed details"})
            continue

        try:
            result = _profile_one(rec, region_lg)
            record = _build_record(rid, rec["title"], seed["source_label"], region_lg, result)
            if args.write:
                upsert_recipe_profiling_trace(record)
            done.add(key)
            n_ok += 1
            pending_ckpt += 1
        except Exception as e:
            n_fail += 1
            append_failure(args.region, {"recipe_id": rid, "reason": str(e)[:500]})

        if pending_ckpt >= args.checkpoint_every:
            if args.write:
                save_checkpoint(args.region, done)
            pending_ckpt = 0

        if i % 25 == 0 or i == len(seeds):
            dt = time.time() - t0
            rate = i / dt if dt > 0 else 0.0
            print(
                f"[regional-reuse:{args.region}] {i}/{len(seeds)} "
                f"(ok={n_ok} fail={n_fail} skip={n_skip}) {rate:.2f} rec/s",
                flush=True,
            )

    if args.write:
        save_checkpoint(args.region, done)
    print(
        f"[regional-reuse:{args.region}] done — ok={n_ok} fail={n_fail} skip={n_skip} "
        f"elapsed={time.time() - t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
