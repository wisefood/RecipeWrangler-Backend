#!/usr/bin/env python3
"""Import the 5 already-scraped, already-normalized web recipe sets into
Neo4j + Postgres + Elasticsearch: SuperValu, The Hungary Soul, Best of
Hungary, Irish Heart Foundation, Slovenian Kitchen.

These JSON files (data/<Dataset>/*.json) match the current importer schema:
id, title, URL, image URL, source, ingredients, tags, dish types, duration,
serves, and method steps. Each source ID is reused as the stable Neo4j
recipe_id.

Per recipe:
  1. Ingredient parse: SuperValu records carry `extra_fields.ingredients_structured`
     (quantity/units/ingredient already split) — used directly, no LLM call.
     The other 4 sources only have raw ingredient text lines — parsed via
     Recipe_Profiling_Chain (LLM), falling back to the deterministic
     split_ingredient_lines() regex splitter on any parse failure.
  2. Neo4j upsert (source = the dataset's real display name, expert_recipe=True).
  3. Nutrition profiling for IE/HU/EU/SI from the same
     parsed ingredients (no re-parse per region) -> Postgres
     nutrients-recipe-profiles.
  4. Elasticsearch: recipe_wrangler.catalog.writer.commit() — the real,
     current project->annotate path.

Resume-safe via checkpoint; one recipe failing does not kill the run.

Usage:
    uv run python scripts/import/web/import_web_scraped_recipes.py --limit 2
    uv run python scripts/import/web/import_web_scraped_recipes.py --write
    uv run python scripts/import/web/import_web_scraped_recipes.py --write --sources supervalu
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)
from recipe_wrangler.repositories.neo4j_recipes import (  # noqa: E402
    upsert_recipe_to_neo4j,
    detect_allergens_from_names,
    driver,
)
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402
from recipe_wrangler.catalog.writer import commit as commit_recipe  # noqa: E402

REGIONS = [("IE", "irish"), ("HU", "hungarian"), ("EU", "eu"), ("SI", "slovenian")]

DATASETS = {
    "supervalu": REPO_ROOT / "data" / "SuperValu" / "supervalu.json",
    "hungarysoul": REPO_ROOT / "data" / "TheHungarySoul" / "thehungarysoul.json",
    "bestofhungary": REPO_ROOT / "data" / "BestOfHungary" / "bestofhungary.json",
    "irishheart": REPO_ROOT / "data" / "IrishHeart" / "irish-heart.json",
    "sloveniankitchen": REPO_ROOT / "data" / "SlovenianKitchen" / "slovenian-kitchen.json",
}

CHECKPOINT = REPO_ROOT / "data" / "checkpoints" / "import_web_scraped_recipes.json"
FAILURES = REPO_ROOT / "data" / "checkpoints" / "import_web_scraped_recipes.failures.jsonl"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    print("\n[import] stop requested — finishing current recipe then exiting.", flush=True)
    _stop = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def load_checkpoint() -> set[str]:
    return set(json.loads(CHECKPOINT.read_text())) if CHECKPOINT.exists() else set()


def save_checkpoint(done: set[str]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(done)))
    tmp.replace(CHECKPOINT)


def append_failure(rec: dict) -> None:
    FAILURES.parent.mkdir(parents=True, exist_ok=True)
    with open(FAILURES, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def set_recipe_url(recipe_id: str, url: str | None) -> None:
    if not url:
        return
    with driver.session() as s:
        s.run(
            "MATCH (r:Recipe {recipe_id: $rid}) SET r.url = $url",
            rid=recipe_id, url=url,
        )


def load_recipes(keys: list[str]) -> list[dict]:
    out: list[dict] = []
    for key in keys:
        path = DATASETS[key]
        if not path.exists():
            print(f"[import] WARN missing {path}", flush=True)
            continue
        recs = json.loads(path.read_text(encoding="utf-8"))
        print(f"[import] {key}: {len(recs)} recipes from {path.name}", flush=True)
        out.extend(recs)
    return out


def _structured_ingredients(rec: dict) -> tuple[list[str], list[str]] | None:
    structured = (rec.get("extra_fields") or {}).get("ingredients_structured")
    if not structured:
        return None
    names: list[str] = []
    measurements: list[str] = []
    for item in structured:
        name = str(item.get("ingredient") or "").strip()
        if not name:
            continue
        note = str(item.get("note") or "").strip()
        if note:
            name = f"{name}, {note}"
        qty = str(item.get("quantity") or "").strip()
        units = str(item.get("units") or "").strip()
        measurement = " ".join(p for p in (qty, units) if p).strip()
        names.append(name)
        measurements.append(measurement or name)
    return (names, measurements) if names else None


def process_recipe(rec: dict, write: bool, *, profile: bool = True) -> str:
    recipe_id = str(rec["id"])
    title = (rec.get("title") or "").strip() or "Untitled Recipe"
    source = (rec.get("source") or "").strip()
    url = rec.get("url")
    image_url = rec.get("image_url")
    try:
        serves = float(rec.get("serves"))
    except (TypeError, ValueError) as exc:
        raise ValueError("missing or invalid serves; review before import") from exc
    if serves <= 0:
        raise ValueError("serves must be positive; review before import")
    duration = float(rec.get("duration") or 0.0)
    ingredient_lines = [str(x).strip() for x in (rec.get("ingredients") or []) if str(x).strip()]
    instructions = [
        str(x).strip()
        for x in ((rec.get("extra_fields") or {}).get("method_steps") or [])
        if str(x).strip()
    ]
    if not ingredient_lines:
        raise ValueError("no ingredients")

    # 1. Ingredient names/measurements: structured data if we have it, else the
    # deterministic splitter. These inputs already contain one ingredient per
    # line; model parsing changed ``1.2 kg`` into ``1200 kg`` in the live graph.
    pre_split = _structured_ingredients(rec)
    ingredient_names: list[str] = []
    measurements: list[str] = []
    profiles: dict[str, dict] = {}

    if pre_split:
        ingredient_names, measurements = pre_split
    else:
        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)

    # 2. Profile every remaining region from the parsed ingredients (no re-parse).
    if profile:
        for region_code, source_key in REGIONS:
            if source_key in profiles:
                continue
            try:
                result = Recipe_Profiling_Chain_Structured.invoke({
                    "title": title, "ingredient_names": ingredient_names,
                    "measurements": measurements, "serves": serves,
                    "total_time": duration, "directions": instructions,
                    "region": region_code, "debug": False,
                })
                profiles[source_key] = result if isinstance(result, dict) else {}
            except Exception as e:
                print(f"    [profile:{region_code}] ERROR {str(e)[:120]}", flush=True)

    if not write:
        return f"DRY ok ingredients={len(ingredient_names)} regions={list(profiles)} serves={serves}"

    # 3. Neo4j upsert + URL.
    allergens = sorted(detect_allergens_from_names(ingredient_names))
    upsert_recipe_to_neo4j(
        recipe_id=recipe_id, title=title, ingredient_lines=ingredient_lines,
        ingredient_names=ingredient_names, measurements=measurements,
        instructions=instructions, duration=duration, serves=serves,
        image_url=image_url, allergens=allergens, user_tags=list(rec.get("tags") or []),
        source=source, source_id=None, expert_recipe=True,
    )
    set_recipe_url(recipe_id, url)

    # 4. Postgres — per-region nutrition profiles.
    now_iso = datetime.now(timezone.utc).isoformat()
    for source_key, result in profiles.items():
        totals = result.get("profiling_totals") or {}
        clean = _extract_clean_totals(totals, f"_{source_key}")
        clean_ps = {k: v / serves for k, v in clean.items()} if clean else None
        upsert_recipe_profiling_trace({
            "recipe_id": recipe_id, "title": title, "source": source,
            "nutrition_source": source_key, "total_nutrients": clean,
            "total_nutrients_per_serving": clean_ps,
            "nutri_score": result.get("nutri_score"), "nutri_score_breakdown": None,
            "nutrition_profiling_details": result.get("ingredients"),
            "nutrition_profiling_debug": result.get("pipeline_trace"),
            "trace": {"profile_result": result, "url": url},
            "pipeline_version": "web_scraped_import_2026-08-21", "computed_at": now_iso,
        })

    # 5. Elasticsearch — real commit path (project only; annotate skipped —
    #    catalog.annotation hardcodes ChatGroq and the org's Groq billing is
    #    delinquent. Recipe is still fully stored/searchable; facets
    #    (cuisines/moods/flavor_profiles/food_groups) are marked pending and
    #    can be backfilled later via `annotate_recipes.py --retry-pending`
    #    once a non-Groq path is wired for that step.)
    commit_result = commit_recipe(recipe_id, annotate_recipe=False)
    return f"WROTE regions={list(profiles)} ingredients={len(ingredient_names)} commit={commit_result.summary()}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="actually write to Neo4j/Postgres/ES")
    ap.add_argument("--limit", type=int, default=None, help="cap total recipes (smoke test)")
    ap.add_argument(
        "--sources", default=",".join(DATASETS),
        help=f"comma-separated subset of {list(DATASETS)}",
    )
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument(
        "--skip-profiles",
        action="store_true",
        help=(
            "Replace Neo4j/Elasticsearch recipe projections without recomputing "
            "PostgreSQL nutrition profiles. Useful for source projection repair."
        ),
    )
    args = ap.parse_args()

    keys = [k.strip().lower() for k in args.sources.split(",") if k.strip()]
    unknown = [k for k in keys if k not in DATASETS]
    if unknown:
        raise SystemExit(f"Unknown source(s) {unknown}. Valid: {list(DATASETS)}")

    recipes = load_recipes(keys)
    if args.limit:
        recipes = recipes[: args.limit]
    print(f"[import] {len(recipes)} recipes queued.", flush=True)

    done = set() if args.no_resume else load_checkpoint()
    if done:
        print(f"[import] {len(done)} already done, skipping.", flush=True)

    n_ok = n_fail = n_skip = 0
    for i, rec in enumerate(recipes, 1):
        if _stop:
            break
        rid = str(rec["id"])
        if rid in done:
            n_skip += 1
            continue
        title = (rec.get("title") or "").strip()
        print(f"[{i}/{len(recipes)}] ({rec.get('source')}) {title}", flush=True)
        try:
            msg = process_recipe(rec, args.write, profile=not args.skip_profiles)
            print(f"    {msg}", flush=True)
            n_ok += 1
            if args.write:
                done.add(rid)
                if n_ok % 10 == 0:
                    save_checkpoint(done)
        except Exception as e:
            n_fail += 1
            print(f"    FAIL {str(e)[:160]}", flush=True)
            append_failure({"recipe_id": rid, "title": title, "reason": str(e)[:300]})

    if args.write:
        save_checkpoint(done)
    print(f"[import] done — ok={n_ok} fail={n_fail} skip={n_skip}", flush=True)


if __name__ == "__main__":
    main()
