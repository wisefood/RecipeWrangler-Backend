#!/usr/bin/env python3
"""Compute EU recipe profiles by reusing prior (USDA/IE/HU) ingredient parse + weights.

For every recipe in ``nutrients-recipe-profiles`` that has a non-EU row but no EU row,
take ``nutrition_profiling_details`` (ingredient name + measurement + weight_g, all
region-independent) from the existing row and run only the EU nutrition + sustainability
match → scale → nutri-score. This skips the recipe-parse LLM and the weight stage
entirely, so no vLLM is needed (unless an EU-side fallback ever calls one, which the
current EU path does not).

Resumable: per-recipe UPSERT + checkpoint file. One failure does not kill the run.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/recompute_eu_from_existing.py --limit 2
    PYTHONPATH=src .venv/bin/python scripts/recompute_eu_from_existing.py --write
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

# Reuse the proven helpers from the main recompute script.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from recompute_all_profiles import (  # noqa: E402
    _build_record,
    _profile_one,
)

REGION = "EU"
OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "recompute_eu_from_existing.checkpoint.json"
FAIL_FILE = OUT_DIR / "recompute_eu_from_existing.failures.jsonl"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    _stop = True
    print("\n[eu-reuse] stop requested — finishing current recipe then exiting.", flush=True)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def load_checkpoint() -> set[str]:
    if CKPT_FILE.exists():
        with open(CKPT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done: set[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CKPT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(done), f)
    tmp.replace(CKPT_FILE)


def append_failure(rec: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAIL_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_seed_rows(limit: int | None, sources: list[str] | None) -> list[dict]:
    """One row per recipe_id that has a non-EU profile and no EU profile yet.

    Picks the most-recent non-EU row as the seed (any non-EU row is fine —
    nutrition_profiling_details fields name/measurement/weight_g are
    region-independent).
    """
    lim = f"LIMIT {int(limit)}" if limit else ""
    src_filter = ""
    params: dict[str, Any] = {}
    if sources:
        src_filter = "AND lower(p.source) = ANY(:sources)"
        params["sources"] = [s.lower() for s in sources]
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
        WHERE p.nutrition_source <> 'eu'
          {src_filter}
          AND NOT EXISTS (
            SELECT 1 FROM "nutrients-recipe-profiles" q
            WHERE q.recipe_id = p.recipe_id AND q.nutrition_source = 'eu'
          )
        ORDER BY p.recipe_id, p.computed_at DESC
        {lim}
        """
    )
    with get_connection() as conn:
        rows = conn.execute(q, params).fetchall()
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
    ap.add_argument("--limit", type=int, default=None,
                    help="cap seed rows fetched (handy for smoke tests)")
    ap.add_argument("--sources", type=str, default=None,
                    help="comma-separated source filter (e.g. recipe1m,healthyfoods)")
    ap.add_argument("--write", action="store_true", help="actually UPSERT to Postgres")
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore checkpoint and re-process everything")
    ap.add_argument("--checkpoint-every", type=int, default=25,
                    help="flush checkpoint every N successful recipes")
    args = ap.parse_args()

    sources = (
        [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.sources else None
    )

    seeds = fetch_seed_rows(args.limit, sources)
    print(f"[eu-reuse] found {len(seeds)} candidate recipes (have non-EU profile, missing EU).",
          flush=True)
    if not seeds:
        return

    done = set() if args.no_resume else load_checkpoint()
    if done:
        print(f"[eu-reuse] checkpoint: {len(done)} already done, will skip.", flush=True)

    n_ok = n_fail = n_skip = 0
    t0 = time.time()
    pending_ckpt = 0

    for i, seed in enumerate(seeds, 1):
        if _stop:
            break
        rid = seed["recipe_id"]
        key = f"{rid}|EU"
        if key in done:
            n_skip += 1
            continue

        rec = build_rec_from_seed(seed)
        if rec is None:
            n_fail += 1
            append_failure({"recipe_id": rid, "reason": "no ingredients in seed details"})
            continue

        try:
            result = _profile_one(rec, REGION)
            record = _build_record(rid, rec["title"], seed["source_label"], REGION, result)
            if args.write:
                upsert_recipe_profiling_trace(record)
            done.add(key)
            n_ok += 1
            pending_ckpt += 1
        except Exception as e:  # one bad recipe must not kill the run
            n_fail += 1
            append_failure({"recipe_id": rid, "reason": str(e)[:500]})

        if pending_ckpt >= args.checkpoint_every:
            if args.write:
                save_checkpoint(done)
            pending_ckpt = 0

        if i % 25 == 0 or i == len(seeds):
            dt = time.time() - t0
            rate = i / dt if dt > 0 else 0.0
            print(
                f"[eu-reuse] {i}/{len(seeds)} (ok={n_ok} fail={n_fail} skip={n_skip}) "
                f"{rate:.2f} rec/s",
                flush=True,
            )

    if args.write:
        save_checkpoint(done)
    print(
        f"[eu-reuse] done — ok={n_ok} fail={n_fail} skip={n_skip} "
        f"elapsed={time.time() - t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
