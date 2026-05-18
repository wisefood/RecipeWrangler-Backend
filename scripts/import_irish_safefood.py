"""
Import Irish SafeFood recipes into Neo4j + Postgres.

For each of 47 recipes:
  1. Clean title (strip AP_ prefix, date suffixes)
  2. Parse duration from prep + cook time strings
  3. Run Recipe_Profiling_Chain (Groq parse + weight + nutrition) for US region
  4. Reuse parsed ingredients for IE and HU regions (structured chain, no re-parse)
  5. Upsert to Neo4j with source='Irish_SafeFood', portion_weight_g stored
  6. Persist US / IE / HU pipeline nutrition profiles + SafeFood ground truth to Postgres
  7. Generate FLUX.1-dev image via HuggingFace Inference API
  8. Update Neo4j image_url; index in Elasticsearch

Checkpoint: scripts/import_irish_safefood.checkpoint.json (resume-safe)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import signal
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from recipe_wrangler.tools.recipe_profiling_chain import (
    Recipe_Profiling_Chain,
    Recipe_Profiling_Chain_Structured,
    split_ingredient_lines,
)
from recipe_wrangler.repositories.neo4j_recipes import (
    upsert_recipe_to_neo4j,
    detect_allergens_from_names,
    resolve_collection_source_id,
)
from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace
from recipe_wrangler.tools.recipe_profiling_tool import _extract_clean_totals

# ── config ────────────────────────────────────────────────────────────────────
EXCEL = Path("data/Irish_SafeFood/Irish Recipes_SafeFood.xlsx")
IMAGE_DIR = Path("data/Irish_SafeFood/images")
IMAGE_URL_PREFIX = "/static/data/Irish_SafeFood/images"
CHECKPOINT = Path("scripts/import_irish_safefood.checkpoint.json")
SOURCE = "Irish_SafeFood"
REGIONS = [("US", "usda"), ("IE", "irish"), ("HU", "hungarian")]

_stop = False

def _handle_signal(sig, frame):
    global _stop
    print("\n[import] Signal received — stopping after current recipe.", flush=True)
    _stop = True

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── helpers ───────────────────────────────────────────────────────────────────

def clean_title(raw: str) -> str:
    """Remove AP_ prefix and date suffixes like _12.01.23."""
    t = re.sub(r"^AP_", "", raw.strip())
    t = re.sub(r"_\d{2}\.\d{2}\.\d{2,4}$", "", t)
    return t.strip()


def parse_minutes(s: str | None) -> float:
    """Convert '15 minutes' or '1 hour 30 minutes' → float minutes."""
    if not s:
        return 0.0
    s = str(s).lower()
    hours = re.search(r"(\d+)\s*hour", s)
    mins  = re.search(r"(\d+)\s*min", s)
    total = 0.0
    if hours:
        total += float(hours.group(1)) * 60
    if mins:
        total += float(mins.group(1))
    return total


def load_checkpoint() -> set:
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()))
    return set()


def save_checkpoint(done: set):
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def generate_recipe_id(title: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{SOURCE}:{title}"))


_flux_pipe = None

def _get_flux_pipe():
    global _flux_pipe
    if _flux_pipe is None:
        import torch
        from diffusers import FluxPipeline
        print("[image] Loading FLUX.1-dev weights (first time)...", flush=True)
        _flux_pipe = FluxPipeline.from_pretrained(
            "black-forest-labs/FLUX.1-dev",
            torch_dtype=torch.bfloat16,
            token=os.getenv("HUGGING_FACE_HUB_TOKEN"),
        ).to("cuda")
        print("[image] FLUX.1-dev loaded.", flush=True)
    return _flux_pipe


def generate_image(title: str, ingredients: str, instructions: str, out_path: Path) -> bool:
    """Generate a recipe photo using FLUX.1-dev locally via diffusers."""
    try:
        pipe = _get_flux_pipe()
        ing_short = ingredients[:400] if ingredients else ""
        prompt = (
            f"Professional food photography for a recipe book. "
            f"{title}. "
            f"Key ingredients: {ing_short}. "
            f"Realistic, appetising, soft natural lighting, shallow depth of field, "
            f"beautifully plated on a clean white plate, top-down angle, "
            f"high resolution, studio quality."
        )
        image = pipe(
            prompt,
            height=768,
            width=768,
            guidance_scale=3.5,
            num_inference_steps=28,
        ).images[0]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(out_path))
        return True
    except Exception as e:
        print(f"  [image] FAILED for '{title}': {e}", flush=True)
        return False


def update_neo4j_image(recipe_id: str, image_url: str):
    from recipe_wrangler.repositories.neo4j_recipes import driver
    with driver.session() as s:
        s.run(
            "MATCH (r:Recipe {recipe_id: $rid}) SET r.image_url = $url",
            rid=recipe_id, url=image_url,
        )


def index_elastic(recipe_id: str, title: str, ingredient_names: list, tags: list):
    try:
        import requests
        from recipe_wrangler.api.config import get_settings
        settings = get_settings()
        url = f"{settings.elastic_url}/{settings.elastic_index}/_doc/{recipe_id}"
        requests.put(
            url,
            json={
                "id": recipe_id,
                "title": title,
                "source": SOURCE,
                "source_id": resolve_collection_source_id(SOURCE),
                "ingredients": ingredient_names,
                "tags": tags,
            },
            timeout=5,
        )
    except Exception:
        pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.load_workbook(EXCEL)
    ws = wb["in"]
    rows = list(ws.iter_rows(min_row=3, values_only=True))
    print(f"[import] {len(rows)} recipes in Excel.", flush=True)

    done = load_checkpoint()
    print(f"[import] {len(done)} already imported.", flush=True)

    errors = 0
    for idx, row in enumerate(rows):
        if _stop:
            break

        (recipe_id_src, raw_title, raw_weight, cooked_weight, yield_factor,
         servings, serving_weight, unit, prep_time, cook_time, cost,
         ingredients_raw, instructions_raw,
         portion_name, portion_measure,
         energy_kj_100g, energy_kcal_100g, fat_100g, satfat_100g, carb_100g,
         sugars_100g, fibre_100g, protein_100g, salt_100g,
         energy_kj_srv, energy_kcal_srv, fat_srv, satfat_srv, carb_srv,
         sugars_srv, fibre_srv, protein_srv, salt_srv) = row

        if not raw_title:
            continue

        title = clean_title(str(raw_title))

        if title in done:
            print(f"[import] Skip (done): {title}", flush=True)
            continue

        print(f"\n[import] [{idx+1}/{len(rows)}] {title}", flush=True)

        serves    = float(servings or 1)
        duration  = parse_minutes(prep_time) + parse_minutes(cook_time)
        # portion_measure is always grams as string e.g. '542g'; serving_weight mixes g and kg
        m = re.search(r"[\d.]+", str(portion_measure or ""))
        srv_weight = float(m.group()) if m else 0.0

        # Ingredient lines (split by newline)
        ingredient_lines = [
            l.strip() for l in str(ingredients_raw or "").split("\n") if l.strip()
        ]
        instructions = [
            l.strip() for l in str(instructions_raw or "").split("\n") if l.strip()
        ]

        recipe_id = generate_recipe_id(title)

        # ── Step 1: Profile all 3 regions ────────────────────────────────────
        profiles: dict[str, dict] = {}
        ingredient_names: list[str] = []
        measurements: list[str] = []

        recipe_text = (
            f"{title}\n\nIngredients:\n"
            + "\n".join(ingredient_lines)
            + "\n\nInstructions:\n"
            + "\n".join(instructions)
        )

        for region_code, source_key in REGIONS:
            try:
                if not ingredient_names:
                    # First region: use full chain with Groq parse
                    print(f"  [profile] {region_code} (with Groq parse)...", flush=True)
                    result = Recipe_Profiling_Chain.invoke({
                        "recipe_text": recipe_text,
                        "debug": False,
                        "region": region_code,
                    })
                    if isinstance(result, dict):
                        ingredient_names = result.get("ingredient_names") or []
                        measurements     = result.get("measurements") or []
                    else:
                        ingredient_names, measurements = split_ingredient_lines(ingredient_lines)
                else:
                    # Subsequent regions: reuse parsed ingredients
                    print(f"  [profile] {region_code} (structured)...", flush=True)
                    result = Recipe_Profiling_Chain_Structured.invoke({
                        "title": title,
                        "ingredient_names": ingredient_names,
                        "measurements": measurements,
                        "serves": serves,
                        "total_time": duration,
                        "directions": instructions,
                        "region": region_code,
                        "debug": False,
                    })

                profiles[source_key] = result if isinstance(result, dict) else {}
                print(f"  [profile] {region_code} done.", flush=True)
            except Exception as e:
                errors += 1
                print(f"  [profile] {region_code} ERROR: {e}", flush=True)
                if not ingredient_names:
                    ingredient_names, measurements = split_ingredient_lines(ingredient_lines)

        # Fallback ingredient parsing if Groq failed entirely
        if not ingredient_names:
            ingredient_names, measurements = split_ingredient_lines(ingredient_lines)

        # ── Step 2: Neo4j upsert ──────────────────────────────────────────────
        try:
            auto_allergens = detect_allergens_from_names(ingredient_names)
            upsert_recipe_to_neo4j(
                recipe_id=recipe_id,
                title=title,
                ingredient_lines=ingredient_lines,
                ingredient_names=ingredient_names,
                measurements=measurements,
                instructions=instructions,
                duration=duration,
                serves=serves,
                image_url=None,
                allergens=sorted(auto_allergens),
                tags=[],
                source=SOURCE,
                source_id=str(recipe_id_src) if recipe_id_src else None,
                expert_recipe=True,
            )
            # Store portion_weight_g as extra property
            from recipe_wrangler.repositories.neo4j_recipes import driver
            with driver.session() as s:
                s.run(
                    "MATCH (r:Recipe {recipe_id: $rid}) SET r.portion_weight_g = $pw, r.has_profile = true",
                    rid=recipe_id, pw=srv_weight,
                )
            print(f"  [neo4j] upserted.", flush=True)
        except Exception as e:
            errors += 1
            print(f"  [neo4j] ERROR: {e}", flush=True)

        # ── Step 3: Postgres — pipeline profiles ──────────────────────────────
        now_iso = datetime.now(timezone.utc).isoformat()
        for source_key, result in profiles.items():
            try:
                totals = result.get("profiling_totals") or {}
                clean = _extract_clean_totals(totals, f"_{source_key}")
                clean_per_serving = {k: v / serves for k, v in clean.items()} if clean else None
                upsert_recipe_profiling_trace({
                    "recipe_id": recipe_id,
                    "title": title,
                    "source": SOURCE,
                    "nutrition_source": source_key,
                    "total_nutrients": clean,
                    "total_nutrients_per_serving": clean_per_serving,
                    "nutri_score": result.get("nutri_score"),
                    "nutri_score_breakdown": None,
                    "nutrition_profiling_details": result.get("ingredients"),
                    "nutrition_profiling_debug": result.get("pipeline_trace"),
                    "trace": {"profile_result": result},
                    "pipeline_version": f"{SOURCE}_pipeline",
                    "computed_at": now_iso,
                })
            except Exception as e:
                errors += 1
                print(f"  [postgres] {source_key} ERROR: {e}", flush=True)

        # ── Step 4: Postgres — SafeFood ground truth ──────────────────────────
        try:
            salt = float(salt_srv or 0)
            ground_truth = {
                "energy_kcal":    float(energy_kcal_srv or 0),
                "fat_g":          float(fat_srv or 0),
                "saturated_fat_g": float(satfat_srv or 0),
                "carbohydrate_g": float(carb_srv or 0),
                "sugar_g":        float(sugars_srv or 0),
                "fibre_g":        float(fibre_srv or 0),
                "protein_g":      float(protein_srv or 0),
                "sodium_mg":      round(salt * 400, 2),  # salt g → sodium mg
            }
            ground_truth_100g = {
                "energy_kcal":    float(energy_kcal_100g or 0),
                "fat_g":          float(fat_100g or 0),
                "saturated_fat_g": float(satfat_100g or 0),
                "carbohydrate_g": float(carb_100g or 0),
                "sugar_g":        float(sugars_100g or 0),
                "fibre_g":        float(fibre_100g or 0),
                "protein_g":      float(protein_100g or 0),
                "sodium_mg":      round(float(salt_100g or 0) * 400, 2),
            }
            ground_truth_trace = {
                "recipe_id": recipe_id,
                "title": title,
                "source": SOURCE,
                "total_nutrients": {k: v * serves for k, v in ground_truth.items()},
                "total_nutrients_per_serving": ground_truth,
                "trace": {"ground_truth_per_serving": ground_truth, "ground_truth_per_100g": ground_truth_100g},
                "nutri_score": None,
                "nutri_score_breakdown": None,
                "nutrition_profiling_details": None,
                "nutrition_profiling_debug": {"source": "excel_ground_truth"},
                "pipeline_version": f"{SOURCE}_ground_truth",
                "computed_at": now_iso,
            }
            # Store SafeFood label values as the explicit reference profile only.
            upsert_recipe_profiling_trace({**ground_truth_trace, "nutrition_source": "safefood"})
            print(f"  [postgres] ground truth stored (safefood).", flush=True)
        except Exception as e:
            errors += 1
            print(f"  [postgres] ground_truth ERROR: {e}", flush=True)

        # ── Step 5: Generate FLUX image ───────────────────────────────────────
        image_path = IMAGE_DIR / f"{recipe_id}.png"
        if not image_path.exists():
            print(f"  [image] Generating FLUX image...", flush=True)
            ok = generate_image(title, "\n".join(ingredient_lines), "\n".join(instructions), image_path)
            if ok:
                image_url = f"{IMAGE_URL_PREFIX}/{image_path.name}"
                update_neo4j_image(recipe_id, image_url)
                print(f"  [image] Saved: {image_path.name}", flush=True)
        else:
            print(f"  [image] Already exists: {image_path.name}", flush=True)
            update_neo4j_image(recipe_id, f"{IMAGE_URL_PREFIX}/{image_path.name}")

        # ── Step 6: Elasticsearch ─────────────────────────────────────────────
        index_elastic(recipe_id, title, ingredient_names, [])

        done.add(title)
        save_checkpoint(done)
        print(f"  [import] Done. errors so far: {errors}", flush=True)

    print(f"\n[import] Finished. {len(done)} imported, {errors} errors.", flush=True)


if __name__ == "__main__":
    main()
