"""Backfill match_confidence / match_reason / similarity into the stored
nutrition_profiling_details of recipe-profile rows.

The profiling pipeline already computes these per ingredient, but they were not
persisted in the `nutrients-recipe-profiles` rows produced by the bulk recompute.
The match is deterministic given (ingredient name, region), so we re-run just the
matcher (`best_nutrition_match`) over the already-stored ingredient names and patch
the JSON column in place — no weight calc, no LLM, no full pipeline.

Usage:
    PYTHONPATH=src python scripts/postgres/backfill_nutrition_match_confidence.py [--write] \
        [--pipeline-version recompute_2026-05-11] [--limit N] [--no-resume]

Default is a dry run (reports counts, writes nothing). Pass --write to persist.
Resumable via data_to_send/backfill_nutrition_match_confidence.checkpoint.json.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.tools.nutrition_match import best_nutrition_match  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import get_engine, _get_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "backfill_nutrition_match_confidence.checkpoint.json"
DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"
MIN_SIMILARITY = 0.5  # matches Recipe_Profiling_Node's payload default

_stop = False


def _handle_stop(_signum, _frame):
    global _stop
    _stop = True
    print("\n[backfill] stop requested — finishing current row then exiting.")


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


# ---- match cache (the same ingredient name recurs across thousands of recipes) ----
_match_cache: dict[tuple[str, str], tuple] = {}


def _match(name: str, source: str) -> tuple:
    key = (name, source)
    cached = _match_cache.get(key)
    if cached is not None:
        return cached
    try:
        m = best_nutrition_match(name, source, MIN_SIMILARITY)
        out = (m.get("confidence"), m.get("reason"), m.get("similarity"), m.get("matched_name"))
    except Exception as exc:  # noqa: BLE001 — never let one ingredient kill the run
        out = ("error", f"matcher_exception:{type(exc).__name__}", None, None)
    _match_cache[key] = out
    return out


def _load_ckpt(enabled: bool) -> set[str]:
    if not enabled or not CKPT_FILE.exists():
        return set()
    try:
        return set(json.loads(CKPT_FILE.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def _save_ckpt(done: set[str], enabled: bool) -> None:
    if not enabled:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CKPT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(CKPT_FILE)


def _patch_details(details: list, source: str) -> tuple[list, int]:
    """Return (patched_details, n_changed)."""
    if not isinstance(details, list):
        return details, 0
    changed = 0
    out = []
    for d in details:
        if not isinstance(d, dict):
            out.append(d)
            continue
        name = d.get("name") or d.get("ingredient") or ""
        conf, reason, sim, matched = _match(str(name), source)
        new = dict(d)
        if new.get("match_confidence") != conf or new.get("match_reason") != reason or new.get("similarity") != sim:
            changed += 1
        new["match_confidence"] = conf
        new["match_reason"] = reason
        new["similarity"] = sim
        # leave the existing matched_nutritional_ingredient untouched; record a
        # mismatch flag if the deterministic re-match disagrees (data drift check)
        existing_matched = new.get("matched_nutritional_ingredient")
        if matched is not None and existing_matched is not None and str(matched) != str(existing_matched):
            new["match_rederive_mismatch"] = {"stored": existing_matched, "rederived": matched}
        out.append(new)
    return out, changed


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true", help="persist changes (default: dry run)")
    p.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--report-every", type=int, default=2000)
    args = p.parse_args(argv)

    eng = get_engine()
    table = _get_config()["profiles_table"]
    ckpt_enabled = args.write
    done = set() if (args.no_resume or not args.write) else _load_ckpt(ckpt_enabled)

    where = "WHERE pipeline_version = :pv"
    params = {"pv": args.pipeline_version}
    with eng.connect() as c:
        total = c.execute(text(f'SELECT count(*) FROM "{table}" {where}'), params).scalar() or 0
        rows = c.execute(
            text(
                f'SELECT recipe_id, nutrition_source, nutrition_profiling_details '
                f'FROM "{table}" {where} ORDER BY recipe_id, nutrition_source'
                + (f" LIMIT {int(args.limit)}" if args.limit else "")
            ),
            params,
        ).all()

    print(f"[backfill] pipeline_version={args.pipeline_version} rows={len(rows)} (table total {total}) "
          f"write={args.write} resume_skip={len(done)}")

    n_seen = n_written = n_skipped = n_changed_entries = n_mismatch_rows = 0
    conf_hist: dict[str, int] = {}
    t0 = time.time()
    engine_w = get_engine()

    for recipe_id, nutrition_source, details in rows:
        if _stop:
            break
        rid_key = f"{recipe_id}|{nutrition_source}"
        if rid_key in done:
            n_skipped += 1
            continue
        n_seen += 1
        source = (nutrition_source or "irish").strip().lower()
        if source not in {"irish", "usda", "hungarian"}:
            source = "irish"
        patched, changed = _patch_details(details or [], source)
        n_changed_entries += changed
        for d in patched if isinstance(patched, list) else []:
            if isinstance(d, dict):
                conf_hist[str(d.get("match_confidence"))] = conf_hist.get(str(d.get("match_confidence")), 0) + 1
                if "match_rederive_mismatch" in d:
                    n_mismatch_rows += 1

        if args.write:
            with engine_w.begin() as wc:
                wc.execute(
                    text(
                        f'UPDATE "{table}" SET nutrition_profiling_details = CAST(:d AS jsonb), '
                        f'updated_at = now() WHERE recipe_id = :rid AND nutrition_source = :ns'
                    ),
                    {"d": json.dumps(patched), "rid": recipe_id, "ns": nutrition_source},
                )
            n_written += 1
            done.add(rid_key)

        if n_seen % args.report_every == 0:
            rate = n_seen / max(1e-6, time.time() - t0)
            remaining = (len(rows) - n_skipped - n_seen) / max(1e-6, rate) / 3600.0
            print(f"[backfill] {n_seen}/{len(rows) - n_skipped} | written={n_written} "
                  f"changed_entries={n_changed_entries} cache={len(_match_cache)} "
                  f"| {rate:.1f}/s ~{remaining:.2f}h left")
            _save_ckpt(done, ckpt_enabled)

    _save_ckpt(done, ckpt_enabled)
    print(f"[backfill] done. seen={n_seen} written={n_written} skipped={n_skipped} "
          f"changed_entries={n_changed_entries} rederive_mismatches={n_mismatch_rows} "
          f"cache_size={len(_match_cache)} in {(time.time()-t0)/3600:.2f}h")
    print(f"[backfill] confidence histogram (over patched ingredient entries): "
          f"{dict(sorted(conf_hist.items(), key=lambda kv: -kv[1]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
