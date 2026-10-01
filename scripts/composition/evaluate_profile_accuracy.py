#!/usr/bin/env python3
"""End-to-end accuracy check: recomputed per-serving nutrition vs the recipe's own published nutrition.

Weights come from ingredient_weight_tool_usda, nutrients from the EU composition profile. Truth: SafeFood
(kcal, fat, saturates, sugars, salt) and HealthyFoods (Calories, Total fat, Saturated fat, Sugar, Sodium).
Run twice with REVIEWED_TABLES_ENABLED=false/true to see what the reviewed tables change.

Usage: PYTHONPATH=src python scripts/composition/evaluate_profile_accuracy.py --out DIR [--healthy-sample 500]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from pathlib import Path

os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")
os.environ.setdefault("RECIPE1M_LLM_FALLBACK_ENABLED", "false")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from recipe_wrangler.tools.ingredient_weight_tool import ingredient_weight_tool_usda  # noqa: E402
from recipe_wrangler.tools.parse_recipe_tool import split_salt_and_pepper_rows  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Tool  # noqa: E402
from scripts.composition.evaluate_salt_oil_policy import SNAP, _num, _raw_records  # noqa: E402
from scripts.prepare_weight_ready_parser_snapshot import _title_key  # noqa: E402

TOTAL_KEYS = {"kcal": "energy_kcal", "fat": "fat_g", "sat_fat": "saturated_fat_g", "sugar": "sugar_g", "sodium": "sodium_mg"}


def _truth(record: dict, dataset: str) -> dict[str, float | None]:
    n = record.get("nutrition") or {}
    if dataset == "safefood":
        salt = _num(n.get("salt_g"))
        return {
            "kcal": _num(n.get("energy_kcal")), "fat": _num(n.get("fat_g")), "sat_fat": _num(n.get("saturates_g")),
            "sugar": _num(n.get("sugars_g")), "sodium": None if salt is None else salt * 400.0,
        }
    return {
        "kcal": _num(n.get("Calories")), "fat": _num(n.get("Total fat")), "sat_fat": _num(n.get("Saturated fat")),
        "sugar": _num(n.get("Sugar")), "sodium": _num(n.get("Sodium")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source", default="eu")
    parser.add_argument("--healthy-sample", type=int, default=500)
    args = parser.parse_args()
    raw = _raw_records()
    rng = random.Random(1)
    rows = []
    for dataset, filename in {"safefood": "safefood_v1_parsed.json", "healthyfoods": "healthyfoods_final_parsed_fixed.json"}.items():
        recipes = json.load(open(SNAP / filename))
        if dataset == "healthyfoods":
            rng.shuffle(recipes)
        used = 0
        for recipe in recipes:
            if dataset == "healthyfoods" and used >= args.healthy_sample:
                break
            truth_rec = raw[dataset].get(_title_key(recipe.get("title")))
            serves = _num((truth_rec or {}).get("serves"))
            truth = _truth(truth_rec or {}, dataset) if truth_rec else {}
            if not serves or not truth.get("kcal"):
                continue
            entries = recipe.get("ingredients") or []
            names, meas, _, _ = split_salt_and_pepper_rows([e["name"] for e in entries], [e["measurement"] for e in entries])
            try:
                weights = ingredient_weight_tool_usda.invoke({"ingredient_names": names, "measurements": meas})
                out = Recipe_Profiling_Tool({
                    "title": recipe["title"], "ingredient_names": names, "measurements": meas,
                    "weights": weights, "serves": serves, "source": args.source,
                })
            except Exception as exc:  # one recipe must not stop the run
                print("FAIL", recipe["title"], exc)
                continue
            totals = out["totals"]
            calc = {
                key: float(totals.get(f"total_{suffix}_{args.source}") or 0) / serves for key, suffix in TOTAL_KEYS.items()
            }
            rows.append({"dataset": dataset, "title": recipe["title"], "serves": serves, "truth": truth, "calc": calc,
                         "total_weight_g": sum(weights)})
            used += 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "rows.json").write_text(json.dumps(rows))
    lines = [f"# Profile accuracy (source={args.source}, REVIEWED_TABLES_ENABLED={os.getenv('REVIEWED_TABLES_ENABLED', 'true')})", ""]
    for dataset in ("safefood", "healthyfoods"):
        sub = [r for r in rows if r["dataset"] == dataset]
        lines.append(f"## {dataset}: {len(sub)} recipes")
        for key in TOTAL_KEYS:
            pairs = [(r["calc"][key], r["truth"][key]) for r in sub if r["truth"].get(key) is not None and r["truth"][key] > 0]
            if not pairs:
                continue
            rel = [abs(c - t) / t for c, t in pairs]
            within = sum(1 for x in rel if x <= 0.25) / len(rel)
            bias = statistics.median(c / t for c, t in pairs)
            lines.append(
                f"- {key}: n={len(pairs)}, median abs error {statistics.median(rel) * 100:.0f}%, "
                f"within 25%: {within * 100:.0f}%, median calc/truth {bias:.2f}"
            )
        lines.append("")
    (args.out / "report.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
