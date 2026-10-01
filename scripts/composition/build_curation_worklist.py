#!/usr/bin/env python3
"""Build the ingredient curation worklist for the 2026-09-16 nutrition pass.

Read-only. Ranks distinct (clean_query'd) ingredient names from the stored EU
profiles two ways — by total recipe mass, and by total sodium/sugar/saturated-fat
contribution — and reports each name's current match so a human can approve or
correct it into data/processed/fallbacks/ingredient_composition_aliases.csv.

See data/analysis/nutrition_curation/PLAN.md (Step 5) for the curation process.

Usage:
    uv run python scripts/composition/build_curation_worklist.py
    uv run python scripts/composition/build_curation_worklist.py --mass-coverage 0.8
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.tools.nutrition_match import clean_query  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import get_connection  # noqa: E402

OUT_DIR = REPO_ROOT / "data/analysis/nutrition_curation"

SQL = """
SELECT
    COALESCE(NULLIF(trim(x->>'ingredient'),''), x->>'name') AS raw_name,
    NULLIF(x->>'weight_g','')::float AS weight_g,
    NULLIF(x->>'sodium_mg','')::float AS sodium_mg,
    NULLIF(x->>'sugar_g','')::float AS sugar_g,
    NULLIF(x->>'saturated_fat_g','')::float AS saturated_fat_g,
    x->>'matched_nutritional_ingredient' AS matched_name,
    x->>'canonical_food_id' AS current_food_id,
    x->>'match_confidence' AS confidence
FROM "nutrients-recipe-profiles" p, jsonb_array_elements(p.nutrition_profiling_details) x
WHERE p.nutrition_source = 'eu'
"""


def _top_names_for_coverage(mass_by_name: dict, coverage: float) -> set[str]:
    total = sum(mass_by_name.values())
    if total <= 0:
        return set()
    ranked = sorted(mass_by_name.items(), key=lambda kv: -kv[1])
    cum = 0.0
    keep: set[str] = set()
    for name, mass in ranked:
        keep.add(name)
        cum += mass
        if cum / total >= coverage:
            break
    return keep


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mass-coverage", type=float, default=0.8,
                         help="Cumulative mass fraction to cover in the mass ranking (default 0.8).")
    parser.add_argument("--top-nutrient", type=int, default=150,
                         help="How many names to take from each nutrient-impact ranking (default 150).")
    args = parser.parse_args()

    with get_connection() as conn:
        rows = conn.execute(text(SQL)).fetchall()

    mass_by_name: dict[str, float] = defaultdict(float)
    sodium_by_name: dict[str, float] = defaultdict(float)
    sugar_by_name: dict[str, float] = defaultdict(float)
    satfat_by_name: dict[str, float] = defaultdict(float)
    lines_by_name: dict[str, int] = defaultdict(int)
    recipes_by_name: dict[str, set] = defaultdict(set)
    sample_match: dict[str, tuple] = {}

    for raw_name, weight_g, sodium_mg, sugar_g, saturated_fat_g, matched_name, current_food_id, confidence in rows:
        if not raw_name:
            continue
        name = clean_query(raw_name) or raw_name.strip().lower()
        lines_by_name[name] += 1
        if weight_g and weight_g > 0:
            mass_by_name[name] += weight_g
        if sodium_mg and sodium_mg > 0:
            sodium_by_name[name] += sodium_mg
        if sugar_g and sugar_g > 0:
            sugar_by_name[name] += sugar_g
        if saturated_fat_g and saturated_fat_g > 0:
            satfat_by_name[name] += saturated_fat_g
        if name not in sample_match:
            sample_match[name] = (matched_name, current_food_id, confidence)

    mass_names = _top_names_for_coverage(mass_by_name, args.mass_coverage)
    nutrient_names = set()
    for by_name in (sodium_by_name, sugar_by_name, satfat_by_name):
        ranked = sorted(by_name.items(), key=lambda kv: -kv[1])[: args.top_nutrient]
        nutrient_names.update(name for name, _ in ranked)

    worklist_names = mass_names | nutrient_names

    def _reason(name: str) -> str:
        reasons = []
        if name in mass_names:
            reasons.append("mass")
        if name in nutrient_names:
            reasons.append("nutrient_impact")
        return "+".join(reasons)

    ranked_worklist = sorted(worklist_names, key=lambda n: -mass_by_name.get(n, 0.0))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "worklist.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "name", "lines", "total_g", "sodium_mg", "sugar_g", "satfat_g",
            "current_match", "current_food_id", "confidence", "rank_reason",
        ])
        for name in ranked_worklist:
            matched_name, current_food_id, confidence = sample_match.get(name, (None, None, None))
            writer.writerow([
                name,
                lines_by_name.get(name, 0),
                round(mass_by_name.get(name, 0.0)),
                round(sodium_by_name.get(name, 0.0)),
                round(sugar_by_name.get(name, 0.0), 1),
                round(satfat_by_name.get(name, 0.0), 1),
                matched_name or "",
                current_food_id or "",
                confidence or "",
                _reason(name),
            ])

    total_mass = sum(mass_by_name.values())
    covered_mass = sum(mass_by_name.get(n, 0.0) for n in ranked_worklist)
    print(f"distinct cleaned names: {len(lines_by_name)}")
    print(f"worklist names: {len(ranked_worklist)} "
          f"(mass-only: {len(mass_names - nutrient_names)}, "
          f"nutrient-only: {len(nutrient_names - mass_names)}, "
          f"both: {len(mass_names & nutrient_names)})")
    print(f"mass covered by worklist: {covered_mass / total_mass:.1%}" if total_mass else "n/a")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
