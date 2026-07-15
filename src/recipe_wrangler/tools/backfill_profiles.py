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


def backfill(regions: tuple[str, ...], limit: int | None, include_recipe1m: bool,
             sleep_s: float, dry_run: bool) -> int:
    from recipe_wrangler.tools.fetch_recipe_info import fetch_recipe_info_by_id
    from recipe_wrangler.tools.recipe_profiling_chain import Recipe_Profiling_Chain_Structured
    # Router import is heavy but guarantees byte-identical persistence with
    # the live background job (clean totals, cache invalidation, ES scores).
    from recipe_wrangler.api.routers.recipes import _persist_profile_trace_best_effort

    ids = _candidate_recipe_ids(include_recipe1m)
    print(f"[backfill] {len(ids)} candidate recipes; regions={list(regions)} "
          f"dry_run={dry_run}", flush=True)

    processed = skipped = failed = 0
    for index, recipe_id in enumerate(ids):
        if _stop or (limit is not None and processed >= limit):
            break

        try:
            missing, reuse_row = _missing_regions(recipe_id, regions)
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill] {recipe_id}: trace lookup failed ({exc}) — skipping", flush=True)
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

        try:
            recipe = fetch_recipe_info_by_id(recipe_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill] {recipe_id}: Neo4j fetch failed ({exc})", flush=True)
            failed += 1
            continue
        if not recipe:
            skipped += 1
            continue

        names, measurements, weights = _chain_inputs(recipe, reuse_row)
        if not names:
            print(f"[backfill] {recipe_id}: no usable ingredients — skipping", flush=True)
            skipped += 1
            continue

        started = time.perf_counter()
        done_regions = []
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
                failed += 1

        processed += 1
        print(f"[backfill] {index + 1}/{len(ids)} {recipe_id}: {done_regions or 'nothing'} "
              f"in {time.perf_counter() - started:.1f}s "
              f"[done={processed} skipped={skipped} failed={failed}]", flush=True)
        if sleep_s:
            time.sleep(sleep_s)

    print(f"[backfill] finished: processed={processed} skipped={skipped} failed={failed}",
          flush=True)
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
                        help="Seconds to pause between recipes (rate limiting)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report which recipes/regions are missing")
    args = parser.parse_args(argv)

    regions = tuple(
        r for r in (part.strip().upper() for part in args.regions.split(","))
        if r in REGIONS
    )
    if not regions:
        parser.error(f"--regions must name at least one of {REGIONS}")
    return backfill(regions, args.limit, args.include_recipe1m, args.sleep, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
