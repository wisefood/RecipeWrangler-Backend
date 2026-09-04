#!/usr/bin/env python3
"""Re-project every recipe from Neo4j+Postgres into Elasticsearch.

Needed after a Postgres-only nutrition write (e.g. `recompute_all_profiles.py`,
which never touches ES) leaves recipes_v4 documents with stale/missing
`nutri_score_*` flat fields. `scripts/maintenance/reconcile.py`'s drift
detection is digest-based against the Neo4j *owner* content only — it cannot
see a Postgres-only profile change, so it won't catch this gap. This script
just re-projects everything unconditionally via `catalog.projection.project_many`
(same function `commit()`/`reconcile.py --apply` uses per-recipe), skipping
the Groq-dependent annotate step entirely (projection only).

Resume-safe via checkpoint; batched with periodic progress printing.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/maintenance/reproject_all_recipes.py
    PYTHONPATH=src .venv/bin/python scripts/maintenance/reproject_all_recipes.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import run_query  # noqa: E402
from recipe_wrangler.catalog.projection import project  # noqa: E402

CHECKPOINT = REPO_ROOT / "scripts" / "maintenance" / "reproject_all_recipes.checkpoint.json"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    print("\n[reproject] stop requested — finishing current batch then exiting.", flush=True)
    _stop = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def all_recipe_ids(limit: int | None) -> list[str]:
    lim = f"LIMIT {int(limit)}" if limit else ""
    rows = run_query(
        f"""
        MATCH (r:Recipe)
        RETURN coalesce(r.recipe_id, r.id) AS recipe_id
        {lim}
        """,
        {},
    )
    return [row["recipe_id"] for row in rows if row.get("recipe_id")]


def load_checkpoint() -> set[str]:
    return set(json.loads(CHECKPOINT.read_text())) if CHECKPOINT.exists() else set()


def save_checkpoint(done: set[str]) -> None:
    tmp = CHECKPOINT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(CHECKPOINT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="cap total recipes (smoke test)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel projections (default: 6)")
    args = ap.parse_args()

    ids = all_recipe_ids(args.limit)
    print(f"[reproject] {len(ids)} recipes in Neo4j", flush=True)

    done = set() if args.no_resume else load_checkpoint()
    if done:
        print(f"[reproject] {len(done)} already done, skipping.", flush=True)
    todo = [rid for rid in ids if rid not in done]

    t0 = time.time()
    n_ok = n_fail = 0
    def reproject_one(rid: str) -> str | None:
        try:
            project(rid, refresh="false")
        except Exception as e:
            return str(e)
        return None

    completed = 0
    next_report = 200
    pending: dict = {}
    executor = ThreadPoolExecutor(max_workers=max(1, args.workers))

    def collect(finished) -> None:
        nonlocal completed, next_report, n_ok, n_fail
        for future in finished:
            rid = pending.pop(future)
            error = future.result()
            completed += 1
            if error is None:
                n_ok += 1
                done.add(rid)
            else:
                n_fail += 1
                print(f"    FAIL {rid}: {error[:160]}", flush=True)
        if completed >= next_report:
            save_checkpoint(done)
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed else 0
            remaining = (len(todo) - completed) / rate if rate else float("inf")
            print(
                f"[reproject] {completed}/{len(todo)} | ok={n_ok} fail={n_fail} "
                f"| {rate:.2f}/s | ~{remaining/3600:.1f}h left",
                flush=True,
            )
            next_report += 200

    try:
        for rid in todo:
            if _stop:
                break
            future = executor.submit(reproject_one, rid)
            pending[future] = rid
            while len(pending) >= max(1, args.workers) * 2:
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                collect(finished)
        while pending:
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            collect(finished)
    finally:
        executor.shutdown(wait=True)

    save_checkpoint(done)
    print(f"[reproject] done — ok={n_ok} fail={n_fail} total_done={len(done)}/{len(ids)}", flush=True)


if __name__ == "__main__":
    main()
