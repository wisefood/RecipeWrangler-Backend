#!/usr/bin/env python3
"""Corpus-wide plausibility audit — read-only, independent of ingredient curation.

Answers "how many OTHER recipes have this shape of bug" without needing every
ingredient name curated first. Two kinds of check:

1. Per-100g bounds on the matched composition record itself: values no real
   food can have (e.g. >40,000mg sodium/100g exceeds pure salt; >900kcal/100g
   exceeds pure fat). A violation here is a MATCH bug, independent of weight.
2. Per-serving totals: values no real serving of food can have (e.g.
   >10,000mg sodium in one serving is 5x the WHO daily limit). A violation
   here can come from a bad match, a bad weight, or their combination — it's
   the outcome a reviewer actually notices, regardless of cause.

Bounds are deliberately conservative (physiologically impossible, not just
unusual) so a flag here is a real defect, not a debatable judgement call.
See data/analysis/nutrition_curation/PLAN.md.

Usage:
    uv run python scripts/composition/audit_profile_plausibility.py
    uv run python scripts/composition/audit_profile_plausibility.py --region eu
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from recipe_wrangler.utils.nutrition_postgres import get_connection  # noqa: E402

OUT_DIR = REPO_ROOT / "data/analysis/nutrition_curation"

# Physiologically impossible per-100g values (real foods never exceed these).
# Pure salt: ~38,758mg sodium/100g. Pure fat/oil: ~884-900kcal/100g.
PER_100G_BOUNDS = {
    "sodium_per_100g_mg": 40000.0,
    "energy_kcal_per_100g": 920.0,
    "sugars_per_100g": 100.0,
    "saturated_fat_per_100g": 100.0,
    "protein_per_100g": 100.0,
    "fibre_per_100g": 100.0,
}

# Implausible for one ingredient's contribution to ONE serving of a recipe.
# WHO daily sodium limit is 2,000mg; 10,000mg from a single ingredient in a
# single serving is the "47,600mg soup" shape of bug, generalized.
PER_LINE_PER_SERVING_BOUNDS = {
    "sodium_mg": 10000.0,
    "energy_kcal": 3000.0,
    "saturated_fat_g": 150.0,
}

SQL = """
SELECT p.recipe_id, p.title, p.nutrition_source, x
FROM "nutrients-recipe-profiles" p, jsonb_array_elements(p.nutrition_profiling_details) x
WHERE p.nutrition_source = :region
"""


def _num(x: dict, key: str) -> float | None:
    v = x.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="eu", choices=["eu", "irish", "hungarian", "slovenian"])
    args = parser.parse_args()

    with get_connection() as conn:
        rows = conn.execute(text(SQL), {"region": args.region}).fetchall()

    per_100g_violations = []
    per_serving_violations = []
    weight_capped_count = 0
    flagged_recipes = set()

    for recipe_id, title, region, detail in rows:
        if not isinstance(detail, dict):
            continue
        if detail.get("weight_capped"):
            weight_capped_count += 1

        for field, bound in PER_100G_BOUNDS.items():
            value = _num(detail, field)
            if value is not None and value > bound:
                per_100g_violations.append({
                    "recipe_id": recipe_id, "title": title, "region": region,
                    "ingredient": detail.get("ingredient") or detail.get("name"),
                    "matched": detail.get("matched_nutritional_ingredient"),
                    "field": field, "value": round(value, 1), "bound": bound,
                })
                flagged_recipes.add(recipe_id)

        for field, bound in PER_LINE_PER_SERVING_BOUNDS.items():
            total = _num(detail, field)
            weight_g = _num(detail, "weight_g")
            # weight_g is *this ingredient's* line weight, not per-serving —
            # the detail dict's own contribution (`sodium_mg` etc.) already
            # reflects this line's absolute contribution to the whole recipe,
            # which for a single ingredient line is a reasonable proxy for
            # "how much of this could land in one serving" at the top end.
            if total is not None and total > bound:
                per_serving_violations.append({
                    "recipe_id": recipe_id, "title": title, "region": region,
                    "ingredient": detail.get("ingredient") or detail.get("name"),
                    "matched": detail.get("matched_nutritional_ingredient"),
                    "weight_g": weight_g, "field": field, "value": round(total, 1), "bound": bound,
                })
                flagged_recipes.add(recipe_id)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def _write(name, rows_):
        if not rows_:
            return
        path = OUT_DIR / name
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_[0].keys()))
            writer.writeheader()
            writer.writerows(rows_)
        print(f"wrote {path} ({len(rows_)} rows)")

    _write(f"plausibility_per_100g_violations_{args.region}.csv", per_100g_violations)
    _write(f"plausibility_per_serving_violations_{args.region}.csv", per_serving_violations)

    n_recipes = len({r[0] for r in rows})
    print(f"\nregion={args.region}  recipes={n_recipes}  lines={len(rows)}")
    print(f"per-100g bound violations: {len(per_100g_violations)} lines")
    print(f"per-line-per-serving bound violations: {len(per_serving_violations)} lines")
    print(f"lines already caught by the concentrate weight cap: {weight_capped_count}")
    print(f"distinct recipes flagged by either check: {len(flagged_recipes)} "
          f"({len(flagged_recipes) / n_recipes:.1%} of recipes)")


if __name__ == "__main__":
    main()
