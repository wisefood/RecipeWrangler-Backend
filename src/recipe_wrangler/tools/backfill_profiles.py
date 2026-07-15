"""Backfill nutrition profiling traces for every recipe and region.

Precomputes what the on-demand background job would otherwise do lazily, so
FoodChat candidates and the recipe UI never wait on a profile:

    python -m recipe_wrangler.tools.backfill_profiles --dry-run
    python -m recipe_wrangler.tools.backfill_profiles --limit 50
    python -m recipe_wrangler.tools.backfill_profiles            # full run

Resume-safe by construction: the profiles table itself is the checkpoint —
recipes already carrying traces for every requested region are skipped, so
re-running after an interruption continues where it stopped.

Weight reuse mirrors the live job: when ANY region's trace exists, its stored
(name, measurement, weight_g) details seed the chain and weight estimation is
skipped; otherwise the first region computes weights and the others reuse them.

recipe1m is excluded by default (FoodChat candidates exclude it too); pass
--include-recipe1m for a full-corpus run.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

REGIONS = ("US", "IE", "HU")
REGION_TO_SOURCE = {"US": "usda", "IE": "irish", "HU": "hungarian"}

_stop = False


def _handle_signal(_sig, _frame):
    global _stop  # noqa: PLW0603
    print("\n[backfill] stop requested — finishing current recipe then exiting.", flush=True)
    _stop = True


def _candidate_recipe_ids(include_recipe1m: bool) -> list[str]:
    from recipe_wrangler.utils.neo4j_utils import run_query

    where = "coalesce(r.status, 'active') <> 'disabled'"
    if not include_recipe1m:
        where += " AND toLower(coalesce(r.source, '')) <> 'recipe1m'"
    rows = run_query(
        f"""
        MATCH (r:Recipe)
        WHERE {where}
        RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
               coalesce(toString(r.source), '') AS source
        ORDER BY source, recipe_id
        """,
        {},
    )
    return [str(row["recipe_id"]) for row in rows if row.get("recipe_id")]


def _missing_regions(recipe_id: str, regions: tuple[str, ...]) -> tuple[list[str], dict | None]:
    """(regions without a usable trace, one existing trace row for weight reuse)."""
    from recipe_wrangler.utils.nutrition_postgres import fetch_recipe_profiling_traces_by_id

    rows = fetch_recipe_profiling_traces_by_id(recipe_id)
    have = {
        str(row.get("nutrition_source") or "").lower()
        for row in rows
        if row.get("nutri_score_breakdown") and row.get("nutrition_profiling_details")
    }
    reuse_row = next((r for r in rows if r.get("nutrition_profiling_details")), None)
    missing = [r for r in regions if REGION_TO_SOURCE[r] not in have]
    return missing, reuse_row


def _chain_inputs(recipe: dict, reuse_row: dict | None):
    """(ingredient_names, measurements, weights|None) — stored details win."""
    from recipe_wrangler.tools.recipe_profiling_chain import split_ingredient_lines

    if reuse_row:
        details = reuse_row.get("nutrition_profiling_details") or []
        names, measurements, weights = [], [], []
        for d in details:
            name = str(d.get("name") or "").strip()
            weight = d.get("weight_g")
            if name and weight is not None:
                names.append(name)
                measurements.append(str(d.get("measurement") or ""))
                weights.append(float(weight))
        if names:
            return names, measurements, weights

    ingredients = recipe.get("ingredients") or []
    lines = [
        f"{i.get('measurement', '')} {i.get('name', '')}".strip() if isinstance(i, dict) else str(i)
        for i in ingredients
    ]
    names, measurements = split_ingredient_lines(lines)
    return names, measurements, None


def _process_recipe(recipe_id: str, missing: list[str], reuse_row: dict | None) -> tuple[list[str], int]:
    """Profile one recipe's missing regions. Returns (done_regions, fail_count)."""
    from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
    from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain_Structured
    from recipe_wrangler.api.routers.recipes import _persist_profile_trace_best_effort

    try:
        recipe = fetch_recipe_info_by_id(recipe_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[backfill] {recipe_id}: Neo4j fetch failed ({exc})", flush=True)
        return [], 1
    if not recipe:
        return [], 0

    names, measurements, weights = _chain_inputs(recipe, reuse_row)
    if not names:
        print(f"[backfill] {recipe_id}: no usable ingredients — skipping", flush=True)
        return [], 0

    done_regions: list[str] = []
    fails = 0
    for region in missing:
        if _stop:
            break
        try:
            result = Recipe_Profiling_Chain_Structured.invoke({
                "title": recipe.get("title", ""),
                "ingredient_names": names,
                "measurements": measurements,
                "serves": float(recipe.get("serves") or 4),
                "total_time": recipe.get("duration"),
                "directions": recipe.get("instructions") or [],
                "region": region,
                "debug": False,
                "weights": weights,
            })
            if not isinstance(result, dict):
                raise RuntimeError(f"unexpected chain payload {type(result).__name__}")
            if weights is None:
                got = result.get("weights")
                if isinstance(got, list) and len(got) == len(names):
                    weights = [float(w or 0.0) for w in got]
            persisted, warning = _persist_profile_trace_best_effort(
                {"recipe_id": recipe_id}, result
            )
            if not persisted:
                raise RuntimeError(warning or "persist failed")
            done_regions.append(region)
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill] {recipe_id} ({region}): FAILED — {exc}", flush=True)
            fails += 1
    return done_regions, fails


def backfill(regions: tuple[str, ...], limit: int | None, include_recipe1m: bool,
             sleep_s: float, dry_run: bool, workers: int = 1) -> int:
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    # Fail fast on import/config problems before spawning anything.
    from recipe_wrangler.api.routers.recipes import _persist_profile_trace_best_effort  # noqa: F401

    ids = _candidate_recipe_ids(include_recipe1m)
    print(f"[backfill] {len(ids)} candidate recipes; regions={list(regions)} "
          f"workers={workers} dry_run={dry_run}", flush=True)

    processed = skipped = failed = 0
    started_at = time.perf_counter()

    def report(recipe_id: str, done_regions: list[str], elapsed: float) -> None:
        print(f"[backfill] {recipe_id}: {done_regions or 'nothing'} in {elapsed:.1f}s "
              f"[done={processed} skipped={skipped} failed={failed} "
              f"elapsed={time.perf_counter() - started_at:.0f}s]", flush=True)

    executor = ThreadPoolExecutor(max_workers=max(1, workers))
    pending: dict = {}
    try:
        for recipe_id in ids:
            if _stop or (limit is not None and processed + len(pending) >= limit):
                break
            try:
                missing, reuse_row = _missing_regions(recipe_id, regions)
            except Exception as exc:  # noqa: BLE001
                print(f"[backfill] {recipe_id}: trace lookup failed ({exc})", flush=True)
                failed += 1
                continue
            if not missing:
                skipped += 1
                continue
            if dry_run:
                processed += 1
                print(f"[backfill] {recipe_id}: missing {missing}"
                      f"{' (weights reusable)' if reuse_row else ''}", flush=True)
                continue

            submit_time = time.perf_counter()
            future = executor.submit(_process_recipe, recipe_id, missing, reuse_row)
            pending[future] = (recipe_id, submit_time)

            # Bounded in-flight set: enumerate lazily, never pile up futures.
            while len(pending) >= max(1, workers) * 2:
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for f in finished:
                    rid, t0 = pending.pop(f)
                    done_regions, fails = f.result()
                    processed += 1
                    failed += fails
                    report(rid, done_regions, time.perf_counter() - t0)
            if sleep_s:
                time.sleep(sleep_s)

        for f in list(pending):
            rid, t0 = pending[f]
            done_regions, fails = f.result()
            processed += 1
            failed += fails
            report(rid, done_regions, time.perf_counter() - t0)
    finally:
        executor.shutdown(wait=True)

    print(f"[backfill] finished: processed={processed} skipped={skipped} failed={failed} "
          f"in {time.perf_counter() - started_at:.0f}s", flush=True)
    return 0 if failed == 0 else 1


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regions", default="US,IE,HU",
                        help="Comma-separated regions to ensure (default all three)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N recipes needing work (default: no limit)")
    parser.add_argument("--include-recipe1m", action="store_true",
                        help="Also backfill the recipe1m corpus (large!)")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="Seconds to pause between submissions (rate limiting)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel recipes (each recipe's regions stay sequential "
                             "so weight reuse holds; 4-6 is a sane ceiling)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report which recipes/regions are missing")
    args = parser.parse_args(argv)

    regions = tuple(
        r for r in (part.strip().upper() for part in args.regions.split(","))
        if r in REGIONS
    )
    if not regions:
        parser.error(f"--regions must name at least one of {REGIONS}")
    return backfill(regions, args.limit, args.include_recipe1m, args.sleep,
                    args.dry_run, workers=args.workers)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
