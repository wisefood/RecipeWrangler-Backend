#!/usr/bin/env python3
"""Compute missing IE/HU profiles for the Recipe1M/HUMMUS reference overlap.

The EU profile is used only as a cache of parsed ingredient names, measurements,
gram weights, and serving count. Parsing and weight estimation are not rerun.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.nutrition_postgres import (  # noqa: E402
    get_connection,
    upsert_recipe_profiling_trace,
)
from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_chroma  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_tool import (  # noqa: E402
    _build_total_nutrients_for_score,
    _resolve_fvl_usda_id,
)
from recipe_wrangler.utils.nutri_score import compute_nutri_score_with_breakdown  # noqa: E402
from recompute_regional_in_place import build_rec_from_seed  # noqa: E402

REGIONS = {"IE": "irish", "HU": "hungarian"}
PIPELINE_VERSION = "recipe1m_reference_balanced_2026-06-12"
OUT_DIR = REPO_ROOT / "data_to_send"

_stop = False


def handle_signal(_sig, _frame) -> None:
    global _stop
    _stop = True
    print("\n[balanced] stop requested; finishing current recipe.", flush=True)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def checkpoint_path(region: str) -> Path:
    return OUT_DIR / f"recipe1m_reference_{region.lower()}.checkpoint.json"


def failure_path(region: str) -> Path:
    return OUT_DIR / f"recipe1m_reference_{region.lower()}.failures.jsonl"


def load_checkpoint(region: str) -> set[str]:
    path = checkpoint_path(region)
    return set(json.loads(path.read_text())) if path.exists() else set()


def save_checkpoint(region: str, done: set[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = checkpoint_path(region)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(path)


def append_failure(region: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with failure_path(region).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def fetch_seeds(region_source: str, limit: int | None, refresh_nonreused: bool) -> list[dict]:
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    query = text(
        f"""
        SELECT eu.recipe_id, eu.title, eu.source,
               eu.nutrition_profiling_details, eu.trace,
               eu.total_sustainability,
               eu.total_sustainability_per_serving,
               eu.sustainability_per_kg,
               eu.sustainability_profiling_details
        FROM "nutrients-recipe-profiles" eu
        JOIN "nutrients-recipe-profiles" ref
          ON ref.recipe_id = eu.recipe_id
         AND ref.source = 'recipe1m'
         AND ref.nutrition_source = 'recipe1m_original'
         AND ref.nutri_score IS NOT NULL
         AND ref.nutri_score <> CAST('null' AS jsonb)
        WHERE eu.source = 'recipe1m'
          AND eu.nutrition_source = 'eu'
          AND eu.nutrition_profiling_details IS NOT NULL
          AND (
            NOT EXISTS (
              SELECT 1
              FROM "nutrients-recipe-profiles" target
              WHERE target.recipe_id = eu.recipe_id
                AND target.source = 'recipe1m'
                AND target.nutrition_source = :region_source
            )
            OR (
              :refresh_nonreused
              AND EXISTS (
                SELECT 1
                FROM "nutrients-recipe-profiles" target
                WHERE target.recipe_id = eu.recipe_id
                  AND target.source = 'recipe1m'
                  AND target.nutrition_source = :region_source
                  AND COALESCE(target.trace->>'weights_reused_from', '') <> 'eu'
              )
            )
          )
        ORDER BY eu.recipe_id
        {limit_sql}
        """
    )
    with get_connection() as connection:
        records = connection.execute(
            query,
            {"region_source": region_source, "refresh_nonreused": refresh_nonreused},
        ).fetchall()
    return [
        {
            "recipe_id": row[0],
            "title": row[1] or "Untitled Recipe",
            "source_label": row[2] or "recipe1m",
            "details": row[3] or [],
            "trace": row[4] or {},
            "total_sustainability": row[5],
            "total_sustainability_per_serving": row[6],
            "sustainability_per_kg": row[7],
            "sustainability_profiling_details": row[8],
        }
        for row in records
    ]


def nutrition_only_record(seed: dict, recipe: dict, region: str) -> dict:
    source = REGIONS[region]
    serves = float(recipe.get("serves") or 1.0)
    names = list(recipe["ingredient_names"])
    measurements = list(recipe["measurements"])
    weights = [float(value or 0.0) for value in recipe["weights"]]
    nutrition = nutritional_tool_chroma.invoke({
        "title": recipe["title"],
        "ingredient_names": names,
        "weights": weights,
        "min_similarity": 0.5,
        "source": source,
        "serves": serves,
    })
    totals = nutrition.get("clean_totals") or {}
    per_serving = nutrition.get("clean_totals_per_serving") or {
        key: float(value) / serves for key, value in totals.items()
    }
    details = []
    nutrition_details = nutrition.get("details") or []
    for index, detail in enumerate(nutrition_details):
        merged = dict(detail)
        merged["name"] = names[index]
        merged["measurement"] = measurements[index] if index < len(measurements) else ""
        merged["weight_g"] = weights[index]
        details.append(merged)

    score_payload = None
    score_breakdown = None
    score_input = _build_total_nutrients_for_score(totals, f"_{source}", serves)
    if score_input:
        score_ingredients = []
        for index, name in enumerate(names[:len(details)]):
            item = {"name": name, "weight_grams": weights[index]}
            usda_id = _resolve_fvl_usda_id(details[index].get("canonical_food_id"), name)
            if usda_id:
                item["usda_id"] = usda_id
            score_ingredients.append(item)
        result = compute_nutri_score_with_breakdown(score_input, score_ingredients)
        if "error" not in result:
            score_breakdown = result.pop("breakdown", None)
            score_payload = result

    matched_weight = sum(
        float(item.get("weight_g") or 0.0)
        for item in details if item.get("matched_nutritional_ingredient")
    )
    total_weight = sum(weights) or 1.0
    nutrition_coverage = round(matched_weight / total_weight, 4)
    return {
        "recipe_id": seed["recipe_id"],
        "title": recipe["title"],
        "source": "recipe1m",
        "nutrition_source": source,
        "total_nutrients": totals,
        "total_nutrients_per_serving": per_serving,
        "nutri_score": score_payload,
        "nutri_score_breakdown": score_breakdown,
        "nutrition_profiling_details": details,
        "nutrition_profiling_debug": None,
        "trace": {
            "serves": serves,
            "serves_source": "reused_from_eu_profile",
            "weights_reused_from": "eu",
            "sustainability_reused_from": "eu",
            "nutrition_coverage": nutrition_coverage,
        },
        "pipeline_version": PIPELINE_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "total_sustainability": seed.get("total_sustainability"),
        "total_sustainability_per_serving": seed.get("total_sustainability_per_serving"),
        "sustainability_per_kg": seed.get("sustainability_per_kg"),
        "sustainability_profiling_details": seed.get("sustainability_profiling_details"),
    }


def run(region: str, write: bool, limit: int | None, no_resume: bool,
        checkpoint_every: int, progress_every: int, refresh_nonreused: bool) -> None:
    region_source = REGIONS[region]
    seeds = fetch_seeds(region_source, limit, refresh_nonreused)
    total = len(seeds)
    done = set() if no_resume else load_checkpoint(region)
    print(
        f"[balanced:{region}] missing={total:,} checkpoint={len(done):,} "
        f"write={write} pipeline={PIPELINE_VERSION}",
        flush=True,
    )
    if not total:
        return

    started = time.time()
    ok = failed = skipped = 0
    for index, seed in enumerate(seeds, 1):
        if _stop:
            break
        recipe_id = seed["recipe_id"]
        if recipe_id in done:
            skipped += 1
            continue
        recipe = build_rec_from_seed(seed)
        if recipe is None:
            failed += 1
            append_failure(region, {"recipe_id": recipe_id, "reason": "no cached ingredient details"})
            continue
        try:
            record = nutrition_only_record(seed, recipe, region)
            if write:
                upsert_recipe_profiling_trace(record)
            done.add(recipe_id)
            ok += 1
        except Exception as exc:  # one recipe must not stop the batch
            failed += 1
            append_failure(region, {"recipe_id": recipe_id, "reason": f"{type(exc).__name__}: {exc}"})

        processed = ok + failed + skipped
        if write and ok and ok % checkpoint_every == 0:
            save_checkpoint(region, done)
        if processed % progress_every == 0 or index == total:
            elapsed = time.time() - started
            rate = processed / elapsed if elapsed else 0.0
            remaining = max(total - processed, 0)
            eta_hours = remaining / rate / 3600 if rate else 0.0
            print(
                f"[balanced:{region}] {processed:,}/{total:,} "
                f"ok={ok:,} failed={failed:,} skipped={skipped:,} "
                f"rate={rate:.2f}/s eta={eta_hours:.2f}h",
                flush=True,
            )

    if write:
        save_checkpoint(region, done)
    elapsed = time.time() - started
    print(
        f"[balanced:{region}] finished {ok + failed + skipped:,}/{total:,} "
        f"ok={ok:,} failed={failed:,} skipped={skipped:,} elapsed={elapsed / 3600:.2f}h",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=sorted(REGIONS), required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--refresh-nonreused", action="store_true",
                        help="also recompute overlap rows not marked as reusing EU weights")
    args = parser.parse_args()
    run(args.region, args.write, args.limit, args.no_resume,
        args.checkpoint_every, args.progress_every, args.refresh_nonreused)


if __name__ == "__main__":
    main()
