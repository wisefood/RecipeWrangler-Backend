"""Backfill per-ingredient weight-resolution trace into recipe-profile rows.

The weight tool already records, per ingredient, *how* a gram weight was
resolved (offline reference / USDA portion tables / FDA / LLM fallback, plus
the portion description and the matched USDA food). That detail was never
persisted into ``nutrients-recipe-profiles``. This script re-derives it.

Two phases (deterministic, no live LLM — ``WEIGHT_LLM`` is cleared so the
weight tool's LLM step fails fast and falls through to the reference cascade):

  * **neo4j sources** (HealthyFoods / MyPlate / FoodHero / Irish_SafeFood) —
    these recipes ran the weight tool during profiling. Re-invoke
    ``ingredient_weight_tool_usda(return_details=True)`` over the stored
    names+measurements, then for each of the recipe's region rows: patch
    ``weight_match_type`` / ``weight_source`` / ``weight_fallback`` /
    ``weight_llm_likely_fired`` (+ ``weight_rederived_g`` on mismatch) onto
    each ingredient entry, and store the full weight-detail blob under
    ``trace.weight_calculation``.
  * **recipe1m** — ships precomputed gram weights, so the pipeline skipped the
    weight tool. Just tag each ingredient entry ``weight_method='dataset_precomputed'``.

Usage:
    PYTHONPATH=src python scripts/postgres/backfill_weight_trace.py [--write] \
        [--pipeline-version recompute_2026-05-11] [--phase neo4j|recipe1m|all] \
        [--limit N] [--no-resume]

Default is a dry run. Resumable via
data_to_send/backfill_weight_trace.checkpoint.json (one entry per recipe_id).
"""

from __future__ import annotations

# --- keep this process small: cap native thread pools before numpy/torch load ---
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "2")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("EMBED_DEVICE", "cpu")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # never touch the GPU

import argparse  # noqa: E402
import json  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()
# Disable the live-LLM weight fallback for the re-derive (it fails fast & is caught).
os.environ["WEIGHT_LLM"] = ""
os.environ["LANGCHAIN_TRACING_V2"] = "false"

try:  # be a good citizen on a shared box
    import torch  # noqa: E402

    torch.set_num_threads(2)
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.tools.ingredient_weight_tool import (  # noqa: E402
    ingredient_weight_tool_usda,
    _detail_source,
)
from recipe_wrangler.utils.nutrition_postgres import get_engine, _get_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "backfill_weight_trace.checkpoint.json"
DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"
NEO4J_SOURCES = ("HealthyFoods", "MyPlate", "FoodHero", "Irish_SafeFood")

_stop = False


def _handle_stop(_signum, _frame):
    global _stop
    _stop = True
    print("\n[weight-trace] stop requested — finishing current recipe then exiting.")


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


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


def _names_measurements(details: list) -> tuple[list[str], list]:
    names, measures = [], []
    for d in details or []:
        if not isinstance(d, dict):
            names.append("")
            measures.append(None)
            continue
        names.append(str(d.get("name") or d.get("ingredient") or ""))
        measures.append(d.get("measurement"))
    return names, measures


def _rederive_weights(names: list[str], measures: list) -> dict | None:
    try:
        res = ingredient_weight_tool_usda.invoke(
            {"ingredient_names": names, "measurements": measures, "return_details": True}
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"weight_tool_exception:{type(exc).__name__}"}
    if isinstance(res, dict):
        return res
    return {"weights": list(res), "details": []}


def _patch_entry_neo4j(entry: dict, wd: dict) -> None:
    entry["weight_match_type"] = wd.get("match_type")
    entry["weight_source"] = _detail_source(wd) if isinstance(wd, dict) else None
    entry["weight_fallback"] = bool(wd.get("fallback"))
    portion = wd.get("portion_match") or {}
    if isinstance(portion, dict) and portion.get("portion_desc"):
        entry["weight_portion_desc"] = portion.get("portion_desc")
    rederived = _f(wd.get("weight_grams"))
    stored = _f(entry.get("weight_g"))
    src = str(wd.get("match_type") or "").lower()
    explicit_llm = bool(wd.get("live_llm_fallback") or wd.get("llm_fallback")) or "llm" in src
    drift = (
        rederived is not None
        and stored is not None
        and stored > 0
        and abs(rederived - stored) > max(0.5, 0.02 * stored)
    )
    entry["weight_llm_likely_fired"] = bool(explicit_llm or drift)
    if drift:
        entry["weight_rederived_g"] = round(rederived, 4)


def _phase_neo4j(eng, table, pv, done, write, limit, report_every) -> None:
    with eng.connect() as c:
        recipes = c.execute(
            text(
                f'SELECT DISTINCT ON (recipe_id) recipe_id, source, nutrition_profiling_details '
                f'FROM "{table}" WHERE pipeline_version = :pv AND source = ANY(:srcs) '
                f'ORDER BY recipe_id, nutrition_source'
                + (f" LIMIT {int(limit)}" if limit else "")
            ),
            {"pv": pv, "srcs": list(NEO4J_SOURCES)},
        ).all()
    print(f"[weight-trace] phase=neo4j recipes={len(recipes)} write={write} resume_skip="
          f"{sum(1 for r in recipes if r[0] in done)}")

    engine_w = get_engine()
    n_seen = n_rows_written = n_llm = n_lenmismatch = n_err = 0
    t0 = time.time()
    for recipe_id, source, details in recipes:
        if _stop:
            break
        if recipe_id in done:
            continue
        n_seen += 1
        names, measures = _names_measurements(details)
        res = _rederive_weights(names, measures)
        wdetails = (res or {}).get("details") or []
        wweights = (res or {}).get("weights") or []
        if res is None or res.get("error"):
            n_err += 1
        # fetch this recipe's region rows
        with engine_w.connect() as c:
            region_rows = c.execute(
                text(
                    f'SELECT nutrition_source, nutrition_profiling_details, trace '
                    f'FROM "{table}" WHERE recipe_id = :rid AND pipeline_version = :pv'
                ),
                {"rid": recipe_id, "pv": pv},
            ).all()
        for nutrition_source, det, trace in region_rows:
            det = det or []
            recipe_llm = False
            if isinstance(det, list) and len(det) == len(wdetails) and wdetails:
                for i, entry in enumerate(det):
                    if isinstance(entry, dict) and isinstance(wdetails[i], dict):
                        _patch_entry_neo4j(entry, wdetails[i])
                        recipe_llm = recipe_llm or bool(entry.get("weight_llm_likely_fired"))
            elif isinstance(det, list):
                n_lenmismatch += 1
                for entry in det:
                    if isinstance(entry, dict):
                        entry["weight_trace_unavailable"] = "ingredient_count_mismatch"
            new_trace = dict(trace or {})
            new_trace["weight_calculation"] = {
                "weights": wweights,
                "details": wdetails,
                "rederived_with_llm_disabled": True,
                "matched_count": sum(
                    1 for x in wdetails if isinstance(x, dict) and x.get("weight_grams") is not None
                ),
                "unmatched_count": sum(
                    1 for x in wdetails if isinstance(x, dict) and x.get("weight_grams") is None
                ),
            }
            if res and res.get("error"):
                new_trace["weight_calculation"]["error"] = res["error"]
            if write:
                with engine_w.begin() as wc:
                    wc.execute(
                        text(
                            f'UPDATE "{table}" SET nutrition_profiling_details = CAST(:d AS jsonb), '
                            f'trace = CAST(:t AS jsonb), updated_at = now() '
                            f'WHERE recipe_id = :rid AND nutrition_source = :ns AND pipeline_version = :pv'
                        ),
                        {"d": json.dumps(det), "t": json.dumps(new_trace), "rid": recipe_id,
                         "ns": nutrition_source, "pv": pv},
                    )
                n_rows_written += 1
            if recipe_llm:
                n_llm += 1
        if write:
            done.add(recipe_id)
        if n_seen % report_every == 0:
            rate = n_seen / max(1e-6, time.time() - t0)
            left = (len(recipes) - n_seen) / max(1e-6, rate) / 3600.0
            print(f"[weight-trace] neo4j {n_seen}/{len(recipes)} | rows_written={n_rows_written} "
                  f"len_mismatch_rows={n_lenmismatch} errs={n_err} | {rate:.1f} rec/s ~{left:.2f}h left")
            _save_ckpt(done, write)
    _save_ckpt(done, write)
    print(f"[weight-trace] phase=neo4j done. recipes_seen={n_seen} rows_written={n_rows_written} "
          f"rows_with_llm_weight={n_llm} len_mismatch_rows={n_lenmismatch} errs={n_err} "
          f"in {(time.time()-t0)/3600:.2f}h")


def _phase_recipe1m(eng, table, pv, write, limit, report_every) -> None:
    with eng.connect() as c:
        rows = c.execute(
            text(
                f'SELECT recipe_id, nutrition_source, nutrition_profiling_details '
                f'FROM "{table}" WHERE pipeline_version = :pv AND source = :src '
                f'ORDER BY recipe_id, nutrition_source'
                + (f" LIMIT {int(limit)}" if limit else "")
            ),
            {"pv": pv, "src": "recipe1m"},
        ).all()
    print(f"[weight-trace] phase=recipe1m rows={len(rows)} write={write}")
    engine_w = get_engine()
    n_seen = n_written = 0
    t0 = time.time()
    for recipe_id, nutrition_source, det in rows:
        if _stop:
            break
        n_seen += 1
        det = det or []
        changed = False
        for entry in det if isinstance(det, list) else []:
            if isinstance(entry, dict) and entry.get("weight_method") != "dataset_precomputed":
                entry["weight_method"] = "dataset_precomputed"
                changed = True
        if write and changed:
            with engine_w.begin() as wc:
                wc.execute(
                    text(
                        f'UPDATE "{table}" SET nutrition_profiling_details = CAST(:d AS jsonb), '
                        f'updated_at = now() WHERE recipe_id = :rid AND nutrition_source = :ns '
                        f'AND pipeline_version = :pv'
                    ),
                    {"d": json.dumps(det), "rid": recipe_id, "ns": nutrition_source, "pv": pv},
                )
            n_written += 1
        if n_seen % report_every == 0:
            rate = n_seen / max(1e-6, time.time() - t0)
            print(f"[weight-trace] recipe1m {n_seen}/{len(rows)} written={n_written} | {rate:.0f}/s")
    print(f"[weight-trace] phase=recipe1m done. seen={n_seen} written={n_written} in {(time.time()-t0)/3600:.2f}h")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true")
    p.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    p.add_argument("--phase", choices=("neo4j", "recipe1m", "all"), default="all")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--report-every", type=int, default=500)
    args = p.parse_args(argv)

    eng = get_engine()
    table = _get_config()["profiles_table"]
    pv = args.pipeline_version
    done = set() if (args.no_resume or not args.write) else _load_ckpt(args.write)

    if args.phase in ("neo4j", "all"):
        _phase_neo4j(eng, table, pv, done, args.write, args.limit, args.report_every)
    if args.phase in ("recipe1m", "all"):
        _phase_recipe1m(eng, table, pv, args.write, args.limit,
                        max(args.report_every, 20000))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
