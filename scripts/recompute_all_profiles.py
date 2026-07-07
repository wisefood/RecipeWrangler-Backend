#!/usr/bin/env python3
"""Recompute nutrition + sustainability for selected recipes across all 3 regions
(IE / HU / US) and upsert them into the ``nutrients-recipe-profiles`` Postgres table.

Scope (default = all of the below):
  * HealthyFoods, MyPlate, FoodHero, Irish_SafeFood  -> ingredients pulled from Neo4j
    (name + HAS_INGREDIENT.measurement), profiled through the full structured pipeline
    (weight tool runs, vLLM as last-resort fallback).
  * recipe1m recipes that have nutrition  -> the ``recipes_with_nutritional_info.json``
    set (~51k). Their pre-computed ``weight_per_ingr`` is fed straight into
    Recipe_Profiling_Node (weight tool skipped) so this is an apples-to-apples
    recompute against the ``recipe1m_original`` ground-truth rows.

It never touches ``nutrition_source = 'recipe1m_original'`` rows (those stay as the
ground-truth baseline).

Resumable: a checkpoint of done ``(recipe_id, region)`` pairs is written next to the
output. Per-recipe try/except — one failure does not kill the run; failures are
collected to a JSON file.

Usage:
    # dry-run a handful per source (no DB writes):
    PYTHONPATH=src .venv/bin/python scripts/recompute_all_profiles.py --limit 5
    # full run, persisting to Postgres:
    PYTHONPATH=src .venv/bin/python scripts/recompute_all_profiles.py --write
    # one source only:
    PYTHONPATH=src .venv/bin/python scripts/recompute_all_profiles.py --write --sources healthyfoods,myplate
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
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_API_KEY", "")
os.environ.setdefault("LANGSMITH_API_KEY", "")

from recipe_wrangler.schemas.models import RecipeState  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain_Structured,
)
from recipe_wrangler.tools.recipe_profiling_tool import (  # noqa: E402
    Recipe_Profiling_Node,
    _extract_clean_totals,
)
from recipe_wrangler.utils.neo4j_utils import run_query  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402

REGIONS = ["IE", "HU", "US", "EU"]
REGION_TO_SOURCE = {"IE": "irish", "HU": "hungarian", "US": "usda", "EU": "eu"}
PIPELINE_VERSION = "recompute_2026-05-11"

NEO4J_SOURCES = {
    "healthyfoods": "HealthyFoods",
    "myplate": "MyPlate",
    "foodhero": "FoodHero",
    "irish_safefood": "Curated Irish Recipes",
}
ALL_SOURCES = list(NEO4J_SOURCES.keys()) + ["recipe1m"]

RECIPE1M_NUTR_JSON = REPO_ROOT / "data" / "processed" / "recipe1m" / "recipes_with_nutritional_info.json"

OUT_DIR = REPO_ROOT / "data_to_send"
CKPT_FILE = OUT_DIR / "recompute_all_profiles.checkpoint.json"
FAIL_FILE = OUT_DIR / "recompute_all_profiles.failures.jsonl"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    _stop = True
    print("\n[recompute] stop requested — finishing current recipe then exiting.", flush=True)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# --------------------------------------------------------------------------- #
# checkpoint
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
# helpers
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
    suffix = f"_{ns_key}"
    totals = r.get("profiling_totals") or {}
    clean_totals = _extract_clean_totals(totals, suffix)
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
        "nutrition_profiling_debug": None,  # skip the big debug blob on purpose
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


def _profile_one(rec: dict, region: str) -> Any:
    """rec keys: recipe_id, title, source_label, ingredient_names, measurements,
    weights (list|None), instructions (list[str]), serves (float|None)."""
    serves = _to_float(rec.get("serves"))
    if rec.get("weights") is not None:
        # pre-computed weights -> feed straight into the profiling node (skip weight tool)
        state = RecipeState(
            title=rec["title"] or "Untitled Recipe",
            ingredient_names=list(rec["ingredient_names"]),
            measurements=list(rec["measurements"]),
            weights=[float(w) if w else 0.0 for w in rec["weights"]],
            serves=serves if serves else 0.0,  # 0 -> pipeline estimates from weight
            region=region,
        )
        return Recipe_Profiling_Node(state)
    return Recipe_Profiling_Chain_Structured.invoke(
        {
            "title": rec["title"] or "Untitled Recipe",
            "ingredient_names": list(rec["ingredient_names"]),
            "measurements": list(rec["measurements"]),
            "serves": serves if serves else 4.0,
            "total_time": None,
            "directions": list(rec.get("instructions") or []),
            "region": region,
            "debug": False,
        }
    )


# --------------------------------------------------------------------------- #
# recipe collection
# --------------------------------------------------------------------------- #
def collect_neo4j_source(src_lower: str, source_label: str, limit: int | None) -> list[dict]:
    lim = f"LIMIT {int(limit)}" if limit else ""
    rows = run_query(
        f"""
        MATCH (r:Recipe) WHERE toLower(coalesce(r.source, '')) = $s
        WITH r {lim}
        MATCH (r)-[h:HAS_INGREDIENT]->(i:Ingredient)
        WITH r, collect({{name: i.name, m: coalesce(h.measurement, '')}}) AS ings
        RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
               r.title AS title, r.instructions AS instructions, r.serves AS serves, ings
        """,
        {"s": src_lower},
    )
    out: list[dict] = []
    for row in rows:
        ings = row["ings"] or []
        names = [str(x["name"] or "").strip() for x in ings if (x.get("name") or "").strip()]
        meas = [str(x["m"] or "").strip() for x in ings if (x.get("name") or "").strip()]
        if not names:
            continue
        out.append(
            {
                "recipe_id": row["recipe_id"],
                "title": row["title"] or "Untitled Recipe",
                "source_label": source_label,
                "ingredient_names": names,
                "measurements": meas,
                "weights": None,
                "instructions": _as_list_of_str(row["instructions"]),
                "serves": _to_float(row["serves"]),
            }
        )
    return out


def collect_recipe1m(limit: int | None) -> list[dict]:
    if not RECIPE1M_NUTR_JSON.exists():
        print(f"[recompute] WARNING: {RECIPE1M_NUTR_JSON} not found — skipping recipe1m.", flush=True)
        return []
    with open(RECIPE1M_NUTR_JSON) as f:
        data = json.load(f)
    if limit:
        data = data[: int(limit)]
    out: list[dict] = []
    for r in data:
        try:
            names = [str(i["text"]) for i in r["ingredients"]]
            meas = [
                f"{q.get('text', '')} {u.get('text', '')}".strip()
                for q, u in zip(r.get("quantity", []), r.get("unit", []))
            ]
            weights = [float(w) if w else 0.0 for w in r.get("weight_per_ingr", [])]
        except Exception:
            continue
        if not names or not weights:
            continue
        out.append(
            {
                "recipe_id": r["id"],
                "title": r.get("title") or "Untitled Recipe",
                "source_label": "recipe1m",
                "ingredient_names": names,
                "measurements": meas,
                "weights": weights,
                "instructions": _as_list_of_str(r.get("instructions")),
                "serves": None,  # recipe1m has no serves -> pipeline estimates
            }
        )
    return out


def collect_recipes(sources: Iterable[str], limit: int | None) -> list[dict]:
    recipes: list[dict] = []
    for src in sources:
        if src == "recipe1m":
            print("[recompute] collecting recipe1m (with-nutrition set)…", flush=True)
            got = collect_recipe1m(limit)
        elif src in NEO4J_SOURCES:
            print(f"[recompute] collecting {NEO4J_SOURCES[src]} from Neo4j…", flush=True)
            got = collect_neo4j_source(src, NEO4J_SOURCES[src], limit)
        else:
            print(f"[recompute] unknown source '{src}' — skipping.", flush=True)
            continue
        print(f"            -> {len(got)} recipes", flush=True)
        recipes.extend(got)
    return recipes


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist to Postgres (default: dry-run)")
    parser.add_argument(
        "--sources",
        default=",".join(ALL_SOURCES),
        help=f"Comma-separated subset of {ALL_SOURCES} (default: all)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap recipes per source (for testing)")
    parser.add_argument("--no-resume", action="store_true", help="Ignore the checkpoint and redo everything")
    parser.add_argument(
        "--checkpoint-every", type=int, default=200, help="Flush checkpoint every N profiling calls"
    )
    args = parser.parse_args()

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    print(f"[recompute] sources={sources} regions={REGIONS} write={args.write} limit={args.limit}", flush=True)

    recipes = collect_recipes(sources, args.limit)
    print(f"[recompute] total recipes to process: {len(recipes)} (×{len(REGIONS)} regions)", flush=True)

    done = set() if (args.no_resume or not args.write) else load_checkpoint()
    if done:
        print(f"[recompute] checkpoint: {len(done)} (recipe_id, region) pairs already done", flush=True)
    ckpt_enabled = args.write  # only persist a resume checkpoint on real write runs

    t0 = time.time()
    n_ok = 0
    n_fail = 0
    n_skip = 0
    n_calls = 0
    total_calls = len(recipes) * len(REGIONS)

    for rec in recipes:
        if _stop:
            break
        rid = rec["recipe_id"]
        for region in REGIONS:
            if _stop:
                break
            key = f"{rid}|{region}"
            if key in done:
                n_skip += 1
                continue
            n_calls += 1
            try:
                result = _profile_one(rec, region)
                record = _build_record(rid, rec["title"], rec["source_label"], region, result)
                if args.write:
                    upsert_recipe_profiling_trace(record)
                n_ok += 1
                done.add(key)
            except Exception as exc:  # noqa: BLE001 — one failure must not kill the run
                n_fail += 1
                append_failure(
                    {
                        "recipe_id": rid,
                        "source": rec["source_label"],
                        "region": region,
                        "error": f"{type(exc).__name__}: {exc}",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )

            if n_calls % args.checkpoint_every == 0:
                save_checkpoint(done, ckpt_enabled)
                elapsed = time.time() - t0
                rate = n_calls / elapsed if elapsed else 0.0
                remaining = (total_calls - n_skip - n_calls) / rate / 3600 if rate else 0.0
                print(
                    f"[recompute] {n_calls}/{total_calls - n_skip} calls | ok={n_ok} fail={n_fail} "
                    f"| {rate:.2f}/s | ~{remaining:.1f}h left",
                    flush=True,
                )

    save_checkpoint(done, ckpt_enabled)
    elapsed = time.time() - t0
    print(
        f"[recompute] done. ok={n_ok} fail={n_fail} skipped={n_skip} in {elapsed/3600:.2f}h. "
        f"write={args.write}. checkpoint={CKPT_FILE} failures={FAIL_FILE if n_fail else '(none)'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
