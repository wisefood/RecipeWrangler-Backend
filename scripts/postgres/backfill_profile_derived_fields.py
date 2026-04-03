#!/usr/bin/env python3
"""Backfill derived recipe profile fields without LLM calls.

Fills, when missing:
1) total_nutrients_per_serving = total_nutrients / serves
2) nutri_score_breakdown from existing nutrient totals + ingredient weights
3) nutri_score from breakdown when missing

Data sources:
- Postgres `nutrients-recipe-profiles` rows
- Neo4j Recipe.serves fallback when serves missing from trace
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from sqlalchemy import text

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

# Suppress LangSmith/LangChain tracing — this script makes no LLM calls and the
# async trace uploads produce rate-limit spam when the monthly quota is exhausted.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from recipe_wrangler.utils.nutrition_postgres import get_connection, _get_config
from recipe_wrangler.utils.neo4j_utils import run_query
from recipe_wrangler.utils.nutri_score import compute_nutri_score_breakdown_from_values
from recipe_wrangler.utils.usda_nutrients_v1 import fruits_veg_legumes_percent
from recipe_wrangler.repositories.chroma_matchers import query_usda_nutrition_candidates

_USDA_MATCH_THRESHOLD = 0.4  # max Chroma distance for a valid USDA food-group match


EXPECTED_KEYS = [
    "protein_g",
    "carbohydrate_g",
    "fat_g",
    "energy_kcal",
    "sugar_g",
    "saturated_fat_g",
    "sodium_mg",
    "fibre_g",
]


def _as_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _as_list(value: object) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
        except Exception:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _build_serves_map(recipe_ids: list[str]) -> dict[str, float]:
    serves_map: dict[str, float] = {}
    for batch in _chunked(recipe_ids, 500):
        # Query 1: string match (works for string-typed recipe_id / id in Neo4j).
        str_rows = run_query(
            """
            UNWIND $ids AS rid
            MATCH (r:Recipe)
            WHERE r.recipe_id = rid OR r.id = rid
            RETURN rid AS recipe_id, r.serves AS serves
            """,
            {"ids": batch},
        )
        for row in str_rows:
            rid = str(row.get("recipe_id") or "").strip()
            serves = _to_float(row.get("serves"))
            if rid and serves and serves > 0:
                serves_map[rid] = serves

        # Query 2: integer match for pure-numeric IDs stored as integers in Neo4j.
        int_batch = []
        for rid in batch:
            if rid not in serves_map:
                try:
                    int_batch.append(int(rid))
                except (ValueError, TypeError):
                    pass
        if int_batch:
            int_rows = run_query(
                """
                UNWIND $ids AS rid
                MATCH (r:Recipe)
                WHERE r.recipe_id = rid OR r.id = rid
                RETURN toString(rid) AS recipe_id, r.serves AS serves
                """,
                {"ids": int_batch},
            )
            for row in int_rows:
                rid = str(row.get("recipe_id") or "").strip()
                serves = _to_float(row.get("serves"))
                if rid and serves and serves > 0:
                    serves_map[rid] = serves
    return serves_map


def _extract_serves_from_trace(trace_obj: dict[str, Any] | None) -> float | None:
    if not isinstance(trace_obj, dict):
        return None

    profile_result = trace_obj.get("profile_result")
    if isinstance(profile_result, dict):
        serves = _to_float(profile_result.get("serves"))
        if serves and serves > 0:
            return serves

    for key in ("foodhero_recipe", "healthyfoods_recipe", "myplate_recipe"):
        recipe_blob = trace_obj.get(key)
        if not isinstance(recipe_blob, dict):
            continue
        serves = _to_float(recipe_blob.get("serves"))
        if serves and serves > 0:
            return serves

    return None


def _source_key(nutrition_source: str) -> str:
    src = (nutrition_source or "").strip().lower()
    if src in {"usda", "irish", "hungarian"}:
        return src
    return src


def _normalize_totals(total_nutrients: dict[str, Any], nutrition_source: str) -> dict[str, float] | None:
    """Return canonical totals with EXPECTED_KEYS.

    Supports:
    - plain keys: protein_g, ...
    - legacy keys: total_protein_g_usda, ...
    """
    plain: dict[str, float] = {}
    for key in EXPECTED_KEYS:
        val = _to_float(total_nutrients.get(key))
        if val is not None:
            plain[key] = val

    if len(plain) == len(EXPECTED_KEYS):
        return plain

    src = _source_key(nutrition_source)
    mapped: dict[str, float] = {}
    for key in EXPECTED_KEYS:
        legacy = f"total_{key}_{src}"
        val = _to_float(total_nutrients.get(legacy))
        if val is not None:
            mapped[key] = val

    if len(mapped) == len(EXPECTED_KEYS):
        return mapped

    return None


def _normalize_per_serving(
    total_nutrients: dict[str, Any],
    per_serving: dict[str, Any] | None,
    nutrition_source: str,
) -> dict[str, float] | None:
    if isinstance(per_serving, dict):
        plain: dict[str, float] = {}
        for key in EXPECTED_KEYS:
            val = _to_float(per_serving.get(key))
            if val is not None:
                plain[key] = val
        if len(plain) == len(EXPECTED_KEYS):
            return plain

    src = _source_key(nutrition_source)
    mapped: dict[str, float] = {}
    for key in EXPECTED_KEYS:
        legacy = f"total_{key}_per_serving_{src}"
        val = _to_float(total_nutrients.get(legacy))
        if val is not None:
            mapped[key] = val
    if len(mapped) == len(EXPECTED_KEYS):
        return mapped
    return None


def _derive_per_serving(totals: dict[str, float], serves: float) -> dict[str, float]:
    return {k: float(v) / serves for k, v in totals.items()}


@lru_cache(maxsize=8192)
def _resolve_usda_id(canonical_food_id: str | None, ingredient_name: str | None) -> str | None:
    """Return a USDA NDB number for food-group classification.

    For USDA-profiled ingredients the canonical_food_id is already an NDB number
    (first two chars are digits, e.g. "11282").  For regional profiles (IE*/HU*)
    we fall back to a Chroma similarity search on the USDA collection.
    """
    if canonical_food_id:
        s = str(canonical_food_id)
        if len(s) >= 2 and s[:2].isdigit():
            return s  # already a USDA NDB number

    # Regional canonical — resolve via ingredient name
    name = (ingredient_name or "").strip()
    if not name:
        return None
    try:
        candidates = query_usda_nutrition_candidates(name)
        if candidates:
            best = candidates[0]
            if best.get("distance", 1.0) < _USDA_MATCH_THRESHOLD:
                return best.get("metadata", {}).get("usda_id")
    except Exception:
        pass
    return None


def _extract_total_weight_and_fvl_ingredients(
    details_obj: list[Any] | None,
) -> tuple[float, list[dict[str, Any]]]:
    if not isinstance(details_obj, list):
        return 0.0, []

    total_weight = 0.0
    fvl_ingredients: list[dict[str, Any]] = []
    for row in details_obj:
        if not isinstance(row, dict):
            continue
        w = _to_float(row.get("weight_g"))
        if w is None or w <= 0:
            continue
        total_weight += w

        ingredient_name = row.get("name") or row.get("ingredient")
        usda_id = _resolve_usda_id(row.get("canonical_food_id"), ingredient_name)
        if usda_id:
            fvl_ingredients.append(
                {
                    "name": row.get("ingredient"),
                    "weight_grams": w,
                    "usda_id": usda_id,
                }
            )

    return total_weight, fvl_ingredients


def _compute_breakdown(
    totals: dict[str, float],
    total_weight_g: float,
    fvl_ingredients: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if total_weight_g <= 0:
        return None

    try:
        nutrient_values = {
            "energy": (float(totals["energy_kcal"]) * 4.184 / total_weight_g) * 100.0,  # kJ/100g
            "sugar": (float(totals["sugar_g"]) / total_weight_g) * 100.0,
            "saturated_fats": (float(totals["saturated_fat_g"]) / total_weight_g) * 100.0,
            "sodium": (float(totals["sodium_mg"]) / total_weight_g) * 100.0,
            "fibers": (float(totals["fibre_g"]) / total_weight_g) * 100.0,
            "proteins": (float(totals["protein_g"]) / total_weight_g) * 100.0,
            "fruit_percentage": (
                fruits_veg_legumes_percent(fvl_ingredients) if fvl_ingredients else 0.0
            ),
        }
        breakdown = compute_nutri_score_breakdown_from_values(nutrient_values, "solid")
        breakdown["inputs"] = {
            "total_weight_g": total_weight_g,
            "ingredients_with_usda_id_count": len(fvl_ingredients),
        }
        return breakdown
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist changes to Postgres.")
    parser.add_argument("--limit", type=int, default=None, help="Optional max rows to scan.")
    parser.add_argument("--force-breakdown", action="store_true", help="Recompute nutri_score_breakdown even if one already exists.")
    parser.add_argument("--skip-serves-map", action="store_true", help="Skip Neo4j serves lookup (use only trace-embedded serves). Speeds up runs where per-serving data already exists.")
    args = parser.parse_args()

    cfg = _get_config()
    table = f"\"{cfg['schema']}\".\"{cfg['profiles_table']}\""

    with get_connection() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT
                    recipe_id,
                    nutrition_source,
                    total_nutrients,
                    total_nutrients_per_serving,
                    nutri_score,
                    nutri_score_breakdown,
                    nutrition_profiling_details,
                    trace
                FROM {table}
                ORDER BY recipe_id, nutrition_source
                """
            )
        ).mappings().all()

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    recipe_ids = sorted({str(r["recipe_id"]) for r in rows if r.get("recipe_id")})
    print(f"loaded rows={len(rows)} unique_recipe_ids={len(recipe_ids)}", flush=True)
    if args.skip_serves_map:
        neo4j_serves: dict[str, float] = {}
        print("skipping neo4j serves map (--skip-serves-map)", flush=True)
    else:
        print("building serves map from neo4j ...", flush=True)
        neo4j_serves = _build_serves_map(recipe_ids)
        print(f"serves_map size={len(neo4j_serves)}", flush=True)

    updated_per_serving = 0
    updated_breakdown = 0
    updated_nutri_score = 0
    untouched = 0
    skipped_no_totals = 0
    skipped_no_serves = 0
    skipped_no_weight = 0

    statements: list[tuple[dict[str, Any], dict[str, Any]]] = []
    total_rows = len(rows)

    for idx, row in enumerate(rows):
        if idx % 500 == 0:
            print(f"processing row {idx}/{total_rows} pending_writes={len(statements)}", flush=True)
        recipe_id = str(row.get("recipe_id") or "").strip()
        nutrition_source = str(row.get("nutrition_source") or "").strip().lower()
        tn = _as_dict(row.get("total_nutrients"))
        if not isinstance(tn, dict):
            skipped_no_totals += 1
            continue

        totals = _normalize_totals(tn, nutrition_source)
        if totals is None:
            skipped_no_totals += 1
            continue

        ps_existing = _as_dict(row.get("total_nutrients_per_serving"))
        ps_norm = _normalize_per_serving(tn, ps_existing, nutrition_source)

        trace_obj = _as_dict(row.get("trace"))
        serves = _extract_serves_from_trace(trace_obj)
        if serves is None:
            serves = neo4j_serves.get(recipe_id)

        new_ps = None
        if ps_norm is None:
            if serves is not None and serves > 0:
                new_ps = _derive_per_serving(totals, serves)
                updated_per_serving += 1
            else:
                skipped_no_serves += 1
        else:
            new_ps = ps_norm

        breakdown_existing = _as_dict(row.get("nutri_score_breakdown"))
        nutri_score_existing = row.get("nutri_score")
        details_obj = _as_list(row.get("nutrition_profiling_details"))
        total_weight_g, fvl_ingredients = _extract_total_weight_and_fvl_ingredients(details_obj)

        new_breakdown = breakdown_existing
        if not isinstance(breakdown_existing, dict) or args.force_breakdown:
            if total_weight_g > 0:
                computed = _compute_breakdown(totals, total_weight_g, fvl_ingredients)
                if isinstance(computed, dict):
                    new_breakdown = computed
                    updated_breakdown += 1
            else:
                skipped_no_weight += 1

        new_nutri_score = nutri_score_existing
        if (new_nutri_score is None or args.force_breakdown) and isinstance(new_breakdown, dict):
            score_payload = {
                "score": new_breakdown.get("score"),
                "nutri_score": new_breakdown.get("nutri_score"),
                "color": new_breakdown.get("color"),
            }
            new_nutri_score = score_payload
            updated_nutri_score += 1

        if new_ps is None and new_breakdown is breakdown_existing and new_nutri_score is nutri_score_existing:
            untouched += 1
            continue

        params = {
            "recipe_id": recipe_id,
            "nutrition_source": nutrition_source,
            "total_nutrients_per_serving": json.dumps(new_ps, separators=(",", ":"))
            if isinstance(new_ps, dict)
            else None,
            "nutri_score_breakdown": json.dumps(new_breakdown, separators=(",", ":"))
            if isinstance(new_breakdown, dict)
            else None,
            "nutri_score": json.dumps(new_nutri_score, separators=(",", ":"))
            if isinstance(new_nutri_score, (dict, list))
            else (json.dumps(new_nutri_score) if new_nutri_score is not None else None),
        }
        statements.append((row, params))

    if args.write and statements:
        with get_connection() as conn:
            tx = conn.begin()
            try:
                for _, p in statements:
                    conn.execute(
                        text(
                            f"""
                            UPDATE {table}
                            SET
                                total_nutrients_per_serving = COALESCE(CAST(:total_nutrients_per_serving AS jsonb), total_nutrients_per_serving),
                                nutri_score_breakdown = COALESCE(CAST(:nutri_score_breakdown AS jsonb), nutri_score_breakdown),
                                nutri_score = COALESCE(CAST(:nutri_score AS jsonb), nutri_score),
                                updated_at = now()
                            WHERE recipe_id = :recipe_id
                              AND nutrition_source = :nutrition_source
                            """
                        ),
                        p,
                    )
                tx.commit()
            except Exception:
                tx.rollback()
                raise

    print(f"table={cfg['schema']}.{cfg['profiles_table']}")
    print(f"scanned_rows={len(rows)}")
    print(f"would_update_rows={len(statements)}")
    print(f"updated_per_serving={updated_per_serving}")
    print(f"updated_breakdown={updated_breakdown}")
    print(f"updated_nutri_score={updated_nutri_score}")
    print(f"untouched_rows={untouched}")
    print(f"skipped_no_totals={skipped_no_totals}")
    print(f"skipped_no_serves={skipped_no_serves}")
    print(f"skipped_no_weight_for_breakdown={skipped_no_weight}")
    print(f"write_mode={bool(args.write)}")


if __name__ == "__main__":
    main()

