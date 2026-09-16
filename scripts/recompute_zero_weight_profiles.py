#!/usr/bin/env python3
"""Re-profile the catalogue rows whose ingredients were stored with weight_g = 0.

Why these rows exist
--------------------
`ingredient_weight_llm_tool` capped every provider call at `max_tokens=48`. That
is ample for a model that answers directly, and fatal for a reasoning family
(gpt-oss, qwen3): the hidden reasoning is charged against the same budget, the
model spends all 48 thinking and returns an EMPTY string. The tool then raises
"Could not parse numeric grams", and the profiler stores `weight_g = 0` for that
ingredient rather than failing the recipe.

Everything downstream is computed from whatever ingredients kept a weight —
nutrition totals, per-serving values, Nutri-Score, and the CO2e attribution that
drives substitution suggestions. One potato-leek soup was profiled from its
potatoes and its black pepper alone, and duly reported that black pepper was
61.7% of its carbon footprint.

The zero rate tracks the provider retirement exactly. On the same ~6,760 recipes,
pipeline `recompute_2026-05-11` went from 22.0% of profiles holding at least one
zero-weight ingredient (June 2026) to 71.6% (August 2026), across Groq's
2026-08-16 shutdown of llama-3.1-8b-instant. The `*_weight_known` pipelines, which
never call the weight tool, sit at 0.0%.

Scope
-----
Rows are selected from Postgres by the defect itself — any profile with at least
one zero-weight ingredient — not by source or date, because the failure cuts
across both. Ingredients and measurements are re-read from Neo4j and the recipe is
re-profiled through the ordinary structured chain, so this run is exactly what the
catalogue would have got had the weight tool been working.

Rows whose `nutrition_source` is `recipe1m_original` are never touched: they are
the ground-truth baseline, and they carry supplied weights rather than estimated
ones.

The preflight
-------------
This script refuses to start until it has watched the weight tool return a sane
number for a handful of known ingredients (`--skip-preflight` overrides, and is
there for a run against a mocked backend, not for production). That check is the
whole lesson of this incident: the August run had no way to notice the tool had
stopped working, so it wrote 17,766 damaged profiles at full speed and reported
success. A batch job that can silently destroy the data it rewrites needs to prove
its instrument works before it touches the first row.

Usage
-----
    # what would change, no writes (default):
    PYTHONPATH=src python scripts/recompute_zero_weight_profiles.py --limit 20

    # a real run:
    PYTHONPATH=src python scripts/recompute_zero_weight_profiles.py --write

    # only the worst rows first:
    PYTHONPATH=src python scripts/recompute_zero_weight_profiles.py --write --max-weight-coverage 0.5
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

from recipe_wrangler.tools.ingredient_weight_llm_tool import (  # noqa: E402
    ingredient_weight_llm_tool,
)
from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain_Structured,
)
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from recipe_wrangler.utils.neo4j_utils import run_query  # noqa: E402
from sqlalchemy import text  # noqa: E402

from recipe_wrangler.utils.nutrition_postgres import (  # noqa: E402
    get_connection,
    upsert_recipe_profiling_trace,
)

#: The four regions the profiler actually supports — `Region` in
#: services/adaptation/schemas.py. `usda`/`US` is NOT one of them; a row is
#: re-profiled for its OWN region, so an unmapped source would quietly be
#: rewritten as EU and lose its regional composition table.
REGION_TO_SOURCE = {"IE": "irish", "HU": "hungarian", "EU": "eu", "SI": "slovenian"}
SOURCE_TO_REGION = {v: k for k, v in REGION_TO_SOURCE.items()}
PIPELINE_VERSION = "recompute_zero_weight_2026-09-16"


def _protected_nutrition_sources() -> frozenset[str]:
    """Sources whose nutrition is SUPPLIED rather than estimated.

    Re-profiling one of these would replace measured values with model
    estimates, which is a worse outcome than the zeros this script repairs.
    Read from the catalogue's own source definitions rather than hardcoded, so a
    source added later is protected without anyone remembering to come here.

    `recipe1m_original` is added explicitly: it is the ground-truth baseline the
    recompute scripts compare against and is not declared in `SOURCES`.
    """
    protected = {"recipe1m_original"}
    try:
        from recipe_wrangler.catalog.sources import SOURCES

        entries = SOURCES.values() if isinstance(SOURCES, dict) else SOURCES
        for entry in entries:
            for name in getattr(entry, "ground_truth_nutrition_sources", None) or ():
                if name:
                    protected.add(str(name))
    except Exception as exc:  # noqa: BLE001
        # Fail loudly rather than silently under-protecting: an empty set here
        # would send the run straight at the measured rows.
        raise SystemExit(
            f"[reprofile] cannot read catalogue source definitions ({exc}); "
            "refusing to run rather than risk overwriting ground-truth nutrition."
        )
    return frozenset(protected)


PROTECTED_NUTRITION_SOURCES = _protected_nutrition_sources()

OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "recompute_zero_weight.checkpoint.json"
FAIL_FILE = OUT_DIR / "recompute_zero_weight.failures.jsonl"
REPORT_FILE = OUT_DIR / "recompute_zero_weight.report.json"

#: (ingredient, quantity, unit, expected grams, tolerance) — deliberately boring
#: cases with an unambiguous answer, so a failure means the tool is broken rather
#: than the question being hard.
PREFLIGHT_CASES = [
    ("milk", "1", "litre", 1000.0, 200.0),
    ("butter", "2", "tbsp", 28.0, 14.0),
    ("garlic", "2", "cloves", 6.0, 6.0),
    ("potatoes", "500", "g", 500.0, 100.0),
]

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    _stop = True
    print("\n[reprofile] stop requested — finishing current recipe then exiting.", flush=True)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def preflight() -> None:
    """Abort unless the weight tool demonstrably works.

    Raises SystemExit rather than returning a flag: there is no sensible way to
    continue, and a run that proceeded "with a warning" is what produced the
    damage this script exists to repair.
    """
    print("[reprofile] preflight: checking the weight tool answers sanely…", flush=True)
    failures: list[str] = []
    for ingredient, qty, unit, expected, tol in PREFLIGHT_CASES:
        try:
            got = ingredient_weight_llm_tool.invoke(
                {"ingredient": ingredient, "parsed_quantity": qty, "parsed_unit": unit}
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{ingredient} {qty} {unit}: raised {type(exc).__name__}: {exc}")
            continue
        try:
            got_f = float(got)
        except (TypeError, ValueError):
            failures.append(f"{ingredient} {qty} {unit}: non-numeric {got!r}")
            continue
        if got_f <= 0:
            failures.append(f"{ingredient} {qty} {unit}: returned {got_f}")
        elif abs(got_f - expected) > tol:
            failures.append(
                f"{ingredient} {qty} {unit}: {got_f}g, expected ~{expected}g (±{tol})"
            )
        else:
            print(f"            ok  {ingredient} {qty} {unit} -> {got_f}g", flush=True)

    if failures:
        print("\n[reprofile] PREFLIGHT FAILED — refusing to run:", flush=True)
        for f in failures:
            print(f"              - {f}", flush=True)
        print(
            "\n            The weight tool is not returning usable numbers, so this run "
            "would\n            rewrite the catalogue with the same zeros it is meant to "
            "repair.\n            Check WEIGHT_LLM (is the model retired?) and "
            "WEIGHT_LLM_MAX_TOKENS\n            (a reasoning model needs far more than 48).",
            flush=True,
        )
        raise SystemExit(2)
    print("[reprofile] preflight passed.\n", flush=True)


# --------------------------------------------------------------------------- #
# checkpoint / failures
# --------------------------------------------------------------------------- #
def load_checkpoint() -> set[str]:
    if CKPT_FILE.exists():
        with open(CKPT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done: set[str], enabled: bool = True) -> None:
    if not enabled:
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CKPT_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(sorted(done), f)
    tmp.replace(CKPT_FILE)


def append_failure(rec: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAIL_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def select_damaged_rows(limit: int | None, max_weight_coverage: float) -> list[dict]:
    """Profiles holding at least one zero-weight ingredient, worst first.

    `jsonb_typeof` is checked before `jsonb_array_length` inside a CASE rather
    than in a WHERE clause: the planner is free to evaluate the length before the
    filter, and some rows store a scalar there, which errors the whole query.
    """
    table = os.getenv("NUTRITION_PROFILES_TABLE", "nutrients-recipe-profiles")
    sql = f"""
        WITH src AS (
            SELECT recipe_id, title, source, nutrition_source, computed_at,
                   CASE WHEN jsonb_typeof(nutrition_profiling_details::jsonb) = 'array'
                        THEN nutrition_profiling_details::jsonb END AS d
            FROM "{table}"
            WHERE nutrition_profiling_details IS NOT NULL
        ), x AS (
            SELECT recipe_id, title, source, nutrition_source, computed_at,
                   jsonb_array_length(d) AS n_ing,
                   (SELECT count(*) FROM jsonb_array_elements(d) e
                     WHERE COALESCE(NULLIF(e->>'weight_g',''),'0')::float > 0) AS n_weighed
            FROM src
            WHERE d IS NOT NULL AND jsonb_array_length(d) > 0
        )
        SELECT recipe_id, title, source, nutrition_source, computed_at,
               n_ing, n_weighed, (n_weighed::float / n_ing) AS weight_coverage
        FROM x
        WHERE n_weighed < n_ing
          AND (n_weighed::float / n_ing) <= :max_cov
          AND COALESCE(nutrition_source, '') <> ALL(:protected)
        ORDER BY (n_weighed::float / n_ing) ASC, recipe_id
    """
    params: dict[str, Any] = {
        "max_cov": max_weight_coverage,
        "protected": list(PROTECTED_NUTRITION_SOURCES),
    }
    if limit:
        sql += " LIMIT :lim"
        params["lim"] = int(limit)

    with get_connection() as conn:
        result = conn.execute(text(sql), params)
        cols = list(result.keys())
        return [dict(zip(cols, row)) for row in result.fetchall()]


def fetch_recipe_inputs(recipe_ids: list[str]) -> dict[str, dict]:
    """Ingredients, measurements, instructions and serves, keyed by recipe id.

    Read from Neo4j rather than from the damaged profile row, because the profile
    stores the RESULT of parsing (a name and a weight) and not the measurement
    string the weight tool needs as its input.
    """
    rows = run_query(
        """
        MATCH (r:Recipe)
        WHERE coalesce(toString(r.recipe_id), toString(r.id)) IN $ids
        MATCH (r)-[h:HAS_INGREDIENT]->(i:Ingredient)
        WITH r, collect({name: i.name, m: coalesce(h.measurement, '')}) AS ings
        RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
               r.title AS title, r.instructions AS instructions,
               r.serves AS serves, ings
        """,
        {"ids": list(recipe_ids)},
    )
    out: dict[str, dict] = {}
    for row in rows:
        ings = row["ings"] or []
        names, meas = [], []
        for x in ings:
            name = str(x.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            meas.append(str(x.get("m") or "").strip())
        if not names:
            continue
        out[str(row["recipe_id"])] = {
            "title": row["title"] or "Untitled Recipe",
            "ingredient_names": names,
            "measurements": meas,
            "instructions": _as_list_of_str(row["instructions"]),
            "serves": _to_float(row["serves"]),
        }
    return out


# --------------------------------------------------------------------------- #
# helpers (kept in step with recompute_all_profiles.py)
# --------------------------------------------------------------------------- #
def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list_of_str(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            if isinstance(v, dict):
                t = v.get("text") or v.get("step") or ""
                if t:
                    out.append(str(t))
            elif v:
                out.append(str(v))
        return out
    return [str(value)]


def _result_to_dict(result: Any) -> dict:
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump(exclude={"raw_recipe", "pipeline_trace"})
    return dict(result)


def _build_record(recipe_id: str, title: str, source_label: str, region: str, result: Any) -> dict:
    r = _result_to_dict(result)
    ns_key = r.get("nutrition_source_key") or REGION_TO_SOURCE[region]
    totals = r.get("profiling_totals") or {}
    clean_totals = _extract_clean_totals(totals, f"_{ns_key}")
    serves = _to_float(r.get("serves")) or 4.0
    clean_per_serving = (
        {k: (v / serves if serves else v) for k, v in clean_totals.items()} if clean_totals else None
    )
    ns = r.get("nutri_score")
    quality = r.get("profiling_quality") or {}
    return {
        "recipe_id": recipe_id,
        "title": title,
        "source": source_label,
        "nutrition_source": r.get("nutrition_source") or REGION_TO_SOURCE[region],
        "total_nutrients": clean_totals,
        "total_nutrients_per_serving": clean_per_serving,
        "nutri_score": ns,
        "nutri_score_breakdown": r.get("nutri_score_breakdown")
        or (ns.get("breakdown") if isinstance(ns, dict) else None),
        "nutrition_profiling_details": r.get("ingredients"),
        "nutrition_profiling_debug": None,
        "trace": {
            "profiling_quality": quality,
            "serves": serves,
            "serves_source": r.get("serves_source"),
            "weights_capped": r.get("weights_capped"),
            "nutrition_coverage": r.get("nutrition_coverage"),
            "sustainability_coverage": r.get("sustainability_coverage"),
        },
        "pipeline_version": PIPELINE_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_sustainability": r.get("total_sustainability"),
        "total_sustainability_per_serving": r.get("total_sustainability_per_serving"),
        "sustainability_per_kg": r.get("sustainability_per_kg"),
        "sustainability_profiling_details": r.get("sustainability_profiling_details"),
    }


def _weighed_fraction(result: Any) -> tuple[int, int]:
    """(weighed, total) ingredient counts in a freshly profiled result."""
    r = _result_to_dict(result)
    ings = r.get("ingredients") or []
    total = len(ings)
    weighed = sum(1 for p in ings if float(p.get("weight_g") or 0.0) > 0.0)
    return weighed, total


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="Persist to Postgres (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="Cap rows selected (for testing)")
    parser.add_argument(
        "--max-weight-coverage",
        type=float,
        default=1.0,
        help="Only rows at or below this weighed/total ratio (e.g. 0.5 = worst half first)",
    )
    parser.add_argument("--no-resume", action="store_true", help="Ignore the checkpoint")
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument(
        "--sleep-ms", type=int, default=0, help="Pause between recipes, to rate-limit the provider"
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the weight-tool check. For mocked backends, not for production.",
    )
    args = parser.parse_args()

    if not args.skip_preflight:
        preflight()

    rows = select_damaged_rows(args.limit, args.max_weight_coverage)
    print(f"[reprofile] selected {len(rows)} damaged profile rows", flush=True)
    if not rows:
        return

    # One Neo4j read for the whole batch rather than per recipe.
    ids = sorted({str(r["recipe_id"]) for r in rows})
    inputs = fetch_recipe_inputs(ids)
    print(f"[reprofile] resolved inputs for {len(inputs)}/{len(ids)} recipes from Neo4j", flush=True)

    done = set() if (args.no_resume or not args.write) else load_checkpoint()
    ckpt_enabled = args.write

    t0 = time.time()
    n_ok = n_fail = n_skip = n_missing = n_unmapped = 0
    improved = worsened = unchanged = 0
    before_ratio_sum = after_ratio_sum = 0.0

    for row in rows:
        if _stop:
            break
        rid = str(row["recipe_id"])
        nsrc = str(row.get("nutrition_source") or "")
        region = SOURCE_TO_REGION.get(nsrc)
        if region is None:
            # Defaulting to EU here would rewrite the row against the wrong
            # composition table — a different kind of damage, quietly applied.
            n_unmapped += 1
            append_failure(
                {"recipe_id": rid, "nutrition_source": nsrc,
                 "error": "nutrition_source has no region mapping — skipped",
                 "ts": datetime.now(timezone.utc).isoformat()}
            )
            continue
        key = f"{rid}|{region}"
        if key in done:
            n_skip += 1
            continue

        rec = inputs.get(rid)
        if not rec:
            n_missing += 1
            append_failure(
                {"recipe_id": rid, "region": region, "error": "no ingredients in Neo4j",
                 "ts": datetime.now(timezone.utc).isoformat()}
            )
            continue

        try:
            result = Recipe_Profiling_Chain_Structured.invoke(
                {
                    "title": rec["title"],
                    "ingredient_names": list(rec["ingredient_names"]),
                    "measurements": list(rec["measurements"]),
                    "serves": rec["serves"] or 4.0,
                    "total_time": None,
                    "directions": list(rec["instructions"]),
                    "region": region,
                    "debug": False,
                }
            )
            weighed, total = _weighed_fraction(result)
            before = float(row["n_weighed"]) / float(row["n_ing"]) if row["n_ing"] else 0.0
            after = (weighed / total) if total else 0.0
            before_ratio_sum += before
            after_ratio_sum += after
            if after > before:
                improved += 1
            elif after < before:
                worsened += 1
            else:
                unchanged += 1

            record = _build_record(rid, rec["title"], str(row.get("source") or ""), region, result)
            if args.write:
                upsert_recipe_profiling_trace(record)
            else:
                print(
                    f"  [dry-run] {rid} {region} weighed {row['n_weighed']}/{row['n_ing']}"
                    f" -> {weighed}/{total}  {rec['title'][:44]}",
                    flush=True,
                )
            n_ok += 1
            done.add(key)
        except Exception as exc:  # noqa: BLE001 — one failure must not kill the run
            n_fail += 1
            append_failure(
                {"recipe_id": rid, "region": region, "error": f"{type(exc).__name__}: {exc}",
                 "ts": datetime.now(timezone.utc).isoformat()}
            )

        if args.sleep_ms:
            time.sleep(args.sleep_ms / 1000.0)

        processed = n_ok + n_fail
        if processed and processed % args.checkpoint_every == 0:
            save_checkpoint(done, ckpt_enabled)
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed else 0.0
            left = (len(rows) - n_skip - processed) / rate / 3600 if rate else 0.0
            print(
                f"[reprofile] {processed}/{len(rows) - n_skip} | ok={n_ok} fail={n_fail} "
                f"| {rate:.2f}/s | ~{left:.1f}h left",
                flush=True,
            )

    save_checkpoint(done, ckpt_enabled)
    elapsed = time.time() - t0
    n_scored = improved + worsened + unchanged
    report = {
        "pipeline_version": PIPELINE_VERSION,
        "write": args.write,
        "selected": len(rows),
        "ok": n_ok,
        "failed": n_fail,
        "skipped_checkpoint": n_skip,
        "missing_in_neo4j": n_missing,
        "unmapped_source": n_unmapped,
        "improved": improved,
        "worsened": worsened,
        "unchanged": unchanged,
        "mean_weight_coverage_before": round(before_ratio_sum / n_scored, 4) if n_scored else None,
        "mean_weight_coverage_after": round(after_ratio_sum / n_scored, 4) if n_scored else None,
        "elapsed_hours": round(elapsed / 3600, 3),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(
        f"\n[reprofile] done. ok={n_ok} fail={n_fail} skipped={n_skip} missing={n_missing} "
        f"in {elapsed/3600:.2f}h (write={args.write})\n"
        f"            weight coverage {report['mean_weight_coverage_before']} -> "
        f"{report['mean_weight_coverage_after']} "
        f"(improved {improved}, worsened {worsened}, unchanged {unchanged})\n"
        f"            report={REPORT_FILE} failures={FAIL_FILE if n_fail else '(none)'}",
        flush=True,
    )
    if worsened:
        print(
            f"[reprofile] NOTE: {worsened} recipes came back with FEWER weighed ingredients. "
            "Inspect those before trusting a full run.",
            flush=True,
        )


if __name__ == "__main__":
    main()
