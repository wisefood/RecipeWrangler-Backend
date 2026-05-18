"""Backfill the point-level nutri_score_breakdown into recipe-profile rows.

The bulk recompute stored the final Nutri-Score grade/score but left the
`nutri_score_breakdown` column NULL. The breakdown is deterministic given the
already-stored `total_nutrients` (recipe totals) plus the per-ingredient weights
and canonical food ids in `nutrition_profiling_details` — no pipeline, no LLM,
no Chroma. This script recomputes it and writes the column in place.

Usage:
    PYTHONPATH=src python scripts/postgres/backfill_nutri_score_breakdown.py [--write] \
        [--pipeline-version recompute_2026-05-11] [--limit N] [--no-resume]

Default is a dry run. Resumable via
data_to_send/backfill_nutri_score_breakdown.checkpoint.json.
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

from recipe_wrangler.utils.nutri_score import (  # noqa: E402
    compute_nutri_score_breakdown_from_values,
)
from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import get_engine, _get_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "backfill_nutri_score_breakdown.checkpoint.json"
DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"

_stop = False


def _handle_stop(_signum, _frame):
    global _stop
    _stop = True
    print("\n[backfill] stop requested — finishing current row then exiting.")


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = _f(d.get(k))
        if v is not None:
            return v
    return None


def _build_breakdown(total_nutrients, details) -> dict | None:
    if not isinstance(total_nutrients, dict):
        return None
    energy_kcal = _pick(total_nutrients, "energy_kcal")
    sugar_g = _pick(total_nutrients, "sugar_g")
    sat_fat_g = _pick(total_nutrients, "saturated_fat_g")
    sodium_mg = _pick(total_nutrients, "sodium_mg")
    fibre_g = _pick(total_nutrients, "fibre_g", "fiber_g")
    protein_g = _pick(total_nutrients, "protein_g")
    if any(v is None for v in (energy_kcal, sugar_g, sat_fat_g, sodium_mg, fibre_g, protein_g)):
        return None

    total_weight_g = 0.0
    fvl: list[dict] = []
    for row in details or []:
        if not isinstance(row, dict):
            continue
        w = _f(row.get("weight_g"))
        if w is None or w <= 0:
            continue
        total_weight_g += w
        name = row.get("name") or row.get("ingredient") or ""
        cfid = row.get("canonical_food_id")
        usda_id = None
        if cfid is not None:
            s = str(cfid)
            if len(s) >= 2 and s[:2].isdigit():
                usda_id = s
        entry = {"name": name, "weight_grams": w}
        if usda_id:
            entry["usda_id"] = usda_id
        fvl.append(entry)

    if total_weight_g <= 0:
        return None

    nutrient_values = {
        "energy": (energy_kcal * 4.184 / total_weight_g) * 100.0,
        "sugar": (sugar_g / total_weight_g) * 100.0,
        "saturated_fats": (sat_fat_g / total_weight_g) * 100.0,
        "sodium": (sodium_mg / total_weight_g) * 100.0,
        "fibers": (fibre_g / total_weight_g) * 100.0,
        "proteins": (protein_g / total_weight_g) * 100.0,
        "fruit_percentage": fruits_veg_legumes_percent(fvl) if fvl else 0.0,
    }
    try:
        bd = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"breakdown_exception:{type(exc).__name__}"}
    bd["inputs"] = {
        "total_weight_g": round(total_weight_g, 2),
        "ingredients_with_usda_id_count": sum(1 for e in fvl if "usda_id" in e),
    }
    return bd


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


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true", help="persist changes (default: dry run)")
    p.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--report-every", type=int, default=5000)
    args = p.parse_args(argv)

    eng = get_engine()
    table = _get_config()["profiles_table"]
    ckpt_enabled = args.write
    done = set() if (args.no_resume or not args.write) else _load_ckpt(ckpt_enabled)

    where = "WHERE pipeline_version = :pv"
    params = {"pv": args.pipeline_version}
    with eng.connect() as c:
        rows = c.execute(
            text(
                f'SELECT recipe_id, nutrition_source, total_nutrients, nutrition_profiling_details '
                f'FROM "{table}" {where} ORDER BY recipe_id, nutrition_source'
                + (f" LIMIT {int(args.limit)}" if args.limit else "")
            ),
            params,
        ).all()

    print(f"[backfill] pipeline_version={args.pipeline_version} rows={len(rows)} "
          f"write={args.write} resume_skip={len(done)}")

    n_seen = n_written = n_skipped = n_ok = n_none = n_err = 0
    grade_hist: dict[str, int] = {}
    t0 = time.time()
    engine_w = get_engine()

    for recipe_id, nutrition_source, total_nutrients, details in rows:
        if _stop:
            break
        rid_key = f"{recipe_id}|{nutrition_source}"
        if rid_key in done:
            n_skipped += 1
            continue
        n_seen += 1
        bd = _build_breakdown(total_nutrients, details)
        if bd is None:
            n_none += 1
        elif isinstance(bd, dict) and bd.get("error"):
            n_err += 1
        else:
            n_ok += 1
            grade_hist[str(bd.get("nutri_score"))] = grade_hist.get(str(bd.get("nutri_score")), 0) + 1

        if args.write:
            with engine_w.begin() as wc:
                wc.execute(
                    text(
                        f'UPDATE "{table}" SET nutri_score_breakdown = CAST(:b AS jsonb), '
                        f'updated_at = now() WHERE recipe_id = :rid AND nutrition_source = :ns'
                    ),
                    {"b": json.dumps(bd) if bd is not None else None, "rid": recipe_id, "ns": nutrition_source},
                )
            n_written += 1
            done.add(rid_key)

        if n_seen % args.report_every == 0:
            rate = n_seen / max(1e-6, time.time() - t0)
            remaining = (len(rows) - n_skipped - n_seen) / max(1e-6, rate) / 3600.0
            print(f"[backfill] {n_seen}/{len(rows) - n_skipped} | written={n_written} "
                  f"ok={n_ok} none={n_none} err={n_err} | {rate:.0f}/s ~{remaining:.2f}h left")
            _save_ckpt(done, ckpt_enabled)

    _save_ckpt(done, ckpt_enabled)
    print(f"[backfill] done. seen={n_seen} written={n_written} skipped={n_skipped} "
          f"ok={n_ok} none={n_none} err={n_err} in {(time.time()-t0)/3600:.2f}h")
    print(f"[backfill] grade histogram: {dict(sorted(grade_hist.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
