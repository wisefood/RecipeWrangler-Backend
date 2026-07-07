#!/usr/bin/env python3
"""Import the scraped safefood.net web recipes into Neo4j + Postgres.

Replaces the legacy 46 Irish_SafeFood spreadsheet recipes with the recipes
scraped from https://www.safefood.net/recipes (see scrape_safefood_recipes.py).

Per recipe:
  1. Profile US / IE / HU / EU via Recipe_Profiling_Chain (Groq parse + weight,
     reusing the parsed ingredients across regions).
  2. Upsert to Neo4j with source='Curated Irish Recipes', the real recipe URL, and the
     real safefood image URL (no FLUX generation).
  3. Persist the region pipeline profiles + the safefood-published per-serving
     nutrition as ground truth to Postgres.
  4. Index in Elasticsearch.

Reuses the same helpers as import_irish_safefood.py — only the data source and
the image/URL handling differ. Resume-safe via checkpoint; one failure does not
kill the run.

Run (Groq for parse+weight, since the vLLM ingredient-tagger is not served):
    PARSE_LLM=llama-3.1-8b-instant WEIGHT_LLM=llama-3.1-8b-instant \
    WEIGHT_LLM_SOURCE=groq \
    PYTHONPATH=src .venv/bin/python scripts/import_safefood_web.py --write
    # dry run (no DB writes), 2 recipes:
    ... scripts/import_safefood_web.py --limit 2
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.tools.recipe_profiling_chain import (  # noqa: E402
    Recipe_Profiling_Chain,
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)
from recipe_wrangler.repositories.neo4j_recipes import (  # noqa: E402
    upsert_recipe_to_neo4j,
    detect_allergens_from_names,
)
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals  # noqa: E402
from safefood_rcsi import (  # noqa: E402
    LAB_NUTRITION_SOURCE,
    lab_total_nutrients,
    load_rcsi_lab_recipes,
    normalize_title,
    rcsi_trace,
)

SOURCE = "Curated Irish Recipes"
REGIONS = [("US", "usda"), ("IE", "irish"), ("HU", "hungarian"), ("EU", "eu")]
SCRAPE_FILES = [
    REPO_ROOT / "exports" / f"safefood_{c}_recipes.json"
    for c in ("breakfast", "lunch", "dinner", "snacks", "desserts")
]
CHECKPOINT = REPO_ROOT / "scripts" / "import_safefood_web.checkpoint.json"
FAILURES = REPO_ROOT / "scripts" / "import_safefood_web.failures.jsonl"

_stop = False


def _handle_signal(_sig, _frame):
    global _stop
    print("\n[import] stop requested — finishing current recipe then exiting.", flush=True)
    _stop = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── helpers ────────────────────────────────────────────────────────────────────


def generate_recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


def parse_minutes(s: str | None) -> float:
    if not s:
        return 0.0
    s = str(s).lower()
    hours = re.search(r"(\d+)\s*(?:hr|hour)", s)
    mins = re.search(r"(\d+)\s*min", s)
    total = 0.0
    if hours:
        total += float(hours.group(1)) * 60
    if mins:
        total += float(mins.group(1))
    return total


def _num(s) -> float | None:
    """First number in a string like '273kcal' / '1.1g' → float."""
    if s is None:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None


def safefood_ground_truth(nutr: dict, serves: float) -> dict | None:
    """Per-serving nutrient dict from the scraped safefood nutrition block.

    The web pages give per-serving energy/fat/saturates/sugars/salt only — no
    per-100g, no protein/carbs/fibre. Sodium is derived from salt (1 g salt ≈
    393.4 mg sodium).
    """
    energy = _num(nutr.get("energy_kcal"))
    fat = _num(nutr.get("fat_g"))
    sat = _num(nutr.get("saturates_g"))
    sugar = _num(nutr.get("sugars_g"))
    salt = _num(nutr.get("salt_g"))
    out: dict[str, float] = {}
    if energy is not None:
        out["energy_kcal"] = energy
    if fat is not None:
        out["fat_g"] = fat
    if sat is not None:
        out["saturated_fat_g"] = sat
    if sugar is not None:
        out["sugar_g"] = sugar
    if salt is not None:
        out["sodium_mg"] = round(salt * 393.4, 1)
    return out or None


_lab_by_normalized_title: dict[str, object] | None = None


def _load_lab_by_normalized_title() -> dict[str, object]:
    global _lab_by_normalized_title
    if _lab_by_normalized_title is None:
        try:
            _lab_by_normalized_title = {
                lab.normalized_title: lab
                for lab in load_rcsi_lab_recipes()
            }
        except Exception as exc:
            print(f"[import] WARN could not load RCSI lab workbook: {exc}", flush=True)
            _lab_by_normalized_title = {}
    return _lab_by_normalized_title


def load_recipes() -> list[dict]:
    recipes: list[dict] = []
    seen_titles: set[str] = set()
    for f in SCRAPE_FILES:
        if not f.exists():
            print(f"[import] WARN missing {f}", flush=True)
            continue
        for rec in json.loads(f.read_text(encoding="utf-8")):
            title = (rec.get("name") or "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            recipes.append(rec)
    return recipes


def load_checkpoint() -> set[str]:
    return set(json.loads(CHECKPOINT.read_text())) if CHECKPOINT.exists() else set()


def save_checkpoint(done: set[str]) -> None:
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def append_failure(rec: dict) -> None:
    with open(FAILURES, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def set_recipe_url(recipe_id: str, url: str | None) -> None:
    if not url:
        return
    from recipe_wrangler.repositories.neo4j_recipes import driver
    with driver.session() as s:
        s.run("MATCH (r:Recipe {recipe_id: $rid}) SET r.url = $url, r.has_profile = true",
              rid=recipe_id, url=url)


def index_elastic(recipe_id: str, title: str, ingredient_names: list, tags: list) -> None:
    try:
        import requests
        from recipe_wrangler.api.config import get_settings
        from recipe_wrangler.repositories.neo4j_recipes import resolve_collection_source_id
        settings = get_settings()
        requests.put(
            f"{settings.elastic_url}/{settings.elastic_index}/_doc/{recipe_id}",
            json={
                "id": recipe_id, "title": title, "source": SOURCE,
                "source_id": resolve_collection_source_id(SOURCE),
                "ingredients": ingredient_names, "tags": tags,
            },
            timeout=5,
        )
    except Exception:
        pass


# ── per-recipe ─────────────────────────────────────────────────────────────────


def process_recipe(rec: dict, write: bool) -> str:
    title = (rec.get("name") or "").strip()
    recipe_id = generate_recipe_id(title)
    url = rec.get("url")
    image_url = rec.get("image_url")
    serves = _num(rec.get("serves")) or 1.0
    duration = (
        parse_minutes(rec.get("total_time"))
        or (parse_minutes(rec.get("prep_time")) + parse_minutes(rec.get("cook_time")))
    )
    ingredient_lines = [str(x).strip() for x in (rec.get("ingredients") or []) if str(x).strip()]
    instructions = [str(x).strip() for x in (rec.get("method") or []) if str(x).strip()]
    if not ingredient_lines:
        raise ValueError("no ingredients")

    recipe_text = (
        f"{title}\n\nIngredients:\n" + "\n".join(ingredient_lines)
        + "\n\nInstructions:\n" + "\n".join(instructions)
    )

    # 1. Parse once via the LLM chain. The structured-parse call is occasionally
    #    rejected by Groq ("Failed to call a function") for some recipes; on any
    #    failure fall back to a deterministic split so a flaky parse never costs
    #    us the whole recipe's profiles.
    profiles: dict[str, dict] = {}
    ingredient_names: list[str] = []
    measurements: list[str] = []
    first_region_code, first_source_key = REGIONS[0]
    try:
        first = Recipe_Profiling_Chain.invoke(
            {"recipe_text": recipe_text, "debug": False, "region": first_region_code}
        )
        if isinstance(first, dict):
            ingredient_names = first.get("ingredient_names") or []
            measurements = first.get("measurements") or []
            if ingredient_names:
                profiles[first_source_key] = first
    except Exception as e:
        print(f"    [parse:{first_region_code}] ERROR {str(e)[:120]}", flush=True)

    if not ingredient_names:
        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)

    # 1b. Profile every remaining region from the parsed ingredients (no re-parse).
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
        return f"DRY ok ingredients={len(ingredient_names)} regions={list(profiles)} serves={serves} dur={duration}"

    # 2. Neo4j upsert + URL.
    allergens = sorted(detect_allergens_from_names(ingredient_names))
    upsert_recipe_to_neo4j(
        recipe_id=recipe_id, title=title, ingredient_lines=ingredient_lines,
        ingredient_names=ingredient_names, measurements=measurements,
        instructions=instructions, duration=duration, serves=serves,
        image_url=image_url, allergens=allergens, tags=[],
        source=SOURCE, source_id=None, expert_recipe=True,
    )
    set_recipe_url(recipe_id, url)

    # 3. Postgres — region pipeline profiles.
    now_iso = datetime.now(timezone.utc).isoformat()
    for source_key, result in profiles.items():
        totals = result.get("profiling_totals") or {}
        clean = _extract_clean_totals(totals, f"_{source_key}")
        clean_ps = {k: v / serves for k, v in clean.items()} if clean else None
        upsert_recipe_profiling_trace({
            "recipe_id": recipe_id, "title": title, "source": SOURCE,
            "nutrition_source": source_key, "total_nutrients": clean,
            "total_nutrients_per_serving": clean_ps,
            "nutri_score": result.get("nutri_score"), "nutri_score_breakdown": None,
            "nutrition_profiling_details": result.get("ingredients"),
            "nutrition_profiling_debug": result.get("pipeline_trace"),
            "trace": {"profile_result": result, "url": url},
            "pipeline_version": f"{SOURCE}_web_pipeline", "computed_at": now_iso,
        })

    # 3b. safefood-published web nutrition. Stored under 'safefood_web' (NOT
    # 'safefood') so it can never overwrite the 47 legacy lab ground-truth rows,
    # which remain the evaluation reference (web pages lack protein/carbs/fibre).
    gt = safefood_ground_truth(rec.get("nutrition") or {}, serves)
    if gt:
        upsert_recipe_profiling_trace({
            "recipe_id": recipe_id, "title": title, "source": SOURCE,
            "nutrition_source": "safefood_web",
            "total_nutrients": {k: v * serves for k, v in gt.items()},
            "total_nutrients_per_serving": gt,
            "nutri_score": None, "nutri_score_breakdown": None,
            "nutrition_profiling_details": None, "nutrition_profiling_debug": None,
            "trace": {"safefood_web": rec.get("nutrition"), "url": url},
            "pipeline_version": f"{SOURCE}_web_groundtruth", "computed_at": now_iso,
        })

    # 3c. RCSI lab nutrition for web recipes that have a lab counterpart.
    # Stored under `safefood_rcsi` and keyed to the web recipe ID, so downstream
    # consumers do not have to switch to the legacy 47-row lab-only recipe set.
    lab = _load_lab_by_normalized_title().get(normalize_title(title))
    if lab:
        match_meta = {"method": "normalized_title", "score": 1.0}
        upsert_recipe_profiling_trace({
            "recipe_id": recipe_id, "title": title, "source": SOURCE,
            "nutrition_source": LAB_NUTRITION_SOURCE,
            "total_nutrients": lab_total_nutrients(lab),
            "total_nutrients_per_serving": lab.ground_truth_per_serving,
            "nutri_score": None, "nutri_score_breakdown": None,
            "nutrition_profiling_details": None,
            "nutrition_profiling_debug": {
                "source": "safefood_rcsi",
                "source_label": "RCSI SafeFood lab nutrition",
                "match": match_meta,
            },
            "trace": rcsi_trace(
                lab,
                match={
                    **match_meta,
                    "web_recipe_id": recipe_id,
                    "web_title": title,
                    "web_url": url,
                },
            ),
            "pipeline_version": "safefood_rcsi_ground_truth",
            "computed_at": now_iso,
        })
        try:
            from recipe_wrangler.repositories.neo4j_recipes import driver
            with driver.session() as s:
                s.run(
                    """
                    MATCH (r:Recipe {recipe_id: $rid})
                    SET r.has_rcsi_lab_nutrition = true,
                        r.ground_truth_nutrition_source = $source,
                        r.rcsi_lab_recipe_id = $lab_recipe_id,
                        r.rcsi_lab_title = $lab_title,
                        r.rcsi_lab_match_method = 'normalized_title',
                        r.rcsi_lab_match_score = 1.0
                    """,
                    rid=recipe_id,
                    source=LAB_NUTRITION_SOURCE,
                    lab_recipe_id=lab.recipe_id_src,
                    lab_title=lab.title,
                )
        except Exception:
            pass

    # 4. Elasticsearch.
    index_elastic(recipe_id, title, ingredient_names, [])
    return (
        f"WROTE regions={list(profiles)} web_gt={'y' if gt else 'n'} "
        f"rcsi_gt={'y' if lab else 'n'} ingredients={len(ingredient_names)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="actually write to Neo4j/Postgres/ES")
    ap.add_argument("--limit", type=int, default=None, help="cap recipes (smoke test)")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    recipes = load_recipes()
    if args.limit:
        recipes = recipes[: args.limit]
    print(f"[import] {len(recipes)} scraped recipes loaded.", flush=True)

    done = set() if args.no_resume else load_checkpoint()
    if done:
        print(f"[import] {len(done)} already done, skipping.", flush=True)

    n_ok = n_fail = n_skip = 0
    for i, rec in enumerate(recipes, 1):
        if _stop:
            break
        title = (rec.get("name") or "").strip()
        rid = generate_recipe_id(title)
        if rid in done:
            n_skip += 1
            continue
        print(f"[{i}/{len(recipes)}] {title}", flush=True)
        try:
            msg = process_recipe(rec, args.write)
            print(f"    {msg}", flush=True)
            n_ok += 1
            if args.write:
                done.add(rid)
                if n_ok % 10 == 0:
                    save_checkpoint(done)
        except Exception as e:
            n_fail += 1
            print(f"    FAIL {str(e)[:160]}", flush=True)
            append_failure({"title": title, "reason": str(e)[:300]})

    if args.write:
        save_checkpoint(done)
    print(f"[import] done — ok={n_ok} fail={n_fail} skip={n_skip}", flush=True)


if __name__ == "__main__":
    main()
