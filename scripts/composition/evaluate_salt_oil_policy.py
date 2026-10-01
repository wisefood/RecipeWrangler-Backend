#!/usr/bin/env python3
"""Choose blank-salt and blank-oil defaults by error against source-truth recipe nutrition.

Only recipes that contain a blank-quantity salt or oil row can tell the scenarios apart, so
only those are profiled. Each recipe is profiled once with the blank rows at 0 g; scenario
totals add ``grams x per-100g`` of the matched row (linear), so no re-profiling is needed.
Truth: SafeFood (kcal, fat, salt g -> sodium mg per serving) and HealthyFoods (Calories, Total fat,
Sodium mg per serving).

Usage: PYTHONPATH=src python scripts/composition/evaluate_salt_oil_policy.py [--source eu|irish]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from pathlib import Path

os.environ.setdefault("LIVE_WEIGHT_LLM_ENABLED", "false")
os.environ.setdefault("RECIPE1M_LLM_FALLBACK_ENABLED", "false")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from recipe_wrangler.tools.ingredient_weight_tool import (  # noqa: E402
    _is_blank_salt,
    ingredient_weight_tool_usda,
)
from recipe_wrangler.tools.parse_recipe_tool import split_salt_and_pepper_rows  # noqa: E402
from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Tool  # noqa: E402
from scripts.prepare_weight_ready_parser_snapshot import _title_key  # noqa: E402

SNAP = REPO / "data/processed/ingredient_parsing_final/2026-09-24/weight_ready_parsed"
OUT = REPO / "data/analysis/salt_oil_policy"
SALT_G = [0.0, 0.3, 0.5, 1.0, 2.0, 5.0]
GREASE_G = [0.0, 2.0, 5.0, 10.0]
FRY_G = [0.0, 5.0, 10.0, 20.0, 40.0]
SALT_SODIUM_MG_PER_100G = 38758.0
OIL_KCAL_PER_100G = 884.0
_GREASE = re.compile(r"greas|non-?stick|brush|oil (?:the|a) |spray|tin\b|tray\b", re.I)
_FRY = re.compile(r"fry|frying|fried|saut|pan\b|sear|roast|drizzl|cook", re.I)


def _num(text) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(match.group()) if match else None


def _truth(record: dict, dataset: str) -> tuple[float | None, float | None, float | None, float | None]:
    n = record.get("nutrition") or {}
    serves = _num(record.get("serves"))
    if dataset == "safefood":
        salt = _num(n.get("salt_g"))
        return serves, _num(n.get("energy_kcal")), _num(n.get("fat_g")), None if salt is None else salt * 400.0
    return serves, _num(n.get("Calories")), _num(n.get("Total fat")), _num(n.get("Sodium"))


def _raw_records() -> dict[str, dict[str, dict]]:
    safefood = {}
    for path in glob.glob(str(REPO / "data/SafeFood_web/*_recipes.json")):
        for rec in json.load(open(path)):
            safefood[_title_key(rec.get("name"))] = rec
    healthy = json.load(open(REPO / "data/HealthyFoods/HealthyFood_recipes.json"))
    healthy = healthy if isinstance(healthy, list) else list(healthy.values())
    return {
        "safefood": safefood,
        "healthyfoods": {_title_key(r.get("title")): r for r in healthy},
    }


def _oil_kind(entry: dict) -> str:
    text = " ".join(str(entry.get(k) or "") for k in ("display", "note"))
    if _GREASE.search(text):
        return "grease"
    return "fry" if _FRY.search(text) else "other"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="eu")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    raw = _raw_records()
    files = {"safefood": "safefood_v1_parsed.json", "healthyfoods": "healthyfoods_final_parsed_fixed.json"}
    rows, done = [], 0
    for dataset, filename in files.items():
        for recipe in json.load(open(SNAP / filename)):
            entries = recipe.get("ingredients") or []
            names = [e["name"] for e in entries]
            meas = [e["measurement"] for e in entries]
            names2, meas2, _, _ = split_salt_and_pepper_rows(names, meas)
            # Entries do not survive the split 1:1; keep the flag by name/measurement.
            blank_salt = [i for i, (n, m) in enumerate(zip(names2, meas2)) if _is_blank_salt(n, m)]
            entry_by_name = {e["name"].lower(): e for e in entries}
            blank_oil = [
                i for i, (n, m) in enumerate(zip(names2, meas2))
                if re.search(r"\boil\b", n, re.I) and not str(m or "").strip()
            ]
            if not blank_salt and not blank_oil:
                continue
            truth = raw[dataset].get(_title_key(recipe.get("title")))
            serves, kcal_t, fat_t, sodium_t = _truth(truth or {}, dataset)
            if not truth or not serves or not (kcal_t or sodium_t):
                continue
            weights = ingredient_weight_tool_usda.invoke({"ingredient_names": names2, "measurements": meas2})
            for i in blank_salt + blank_oil:
                weights[i] = 0.0
            try:
                out = Recipe_Profiling_Tool({
                    "title": recipe["title"], "ingredient_names": names2, "measurements": meas2,
                    "weights": weights, "serves": serves, "source": args.source,
                })
            except Exception as exc:  # one recipe must not stop the run
                print("FAIL", recipe["title"], exc)
                continue
            totals = out["totals"]
            suffix = f"_{args.source}"
            base = {
                "sodium": float(totals.get(f"total_sodium_mg{suffix}") or 0) / serves,
                "kcal": float(totals.get(f"total_energy_kcal{suffix}") or 0) / serves,
                "fat": float(totals.get(f"total_fat_g{suffix}") or 0) / serves,
            }
            ing = out["ingredients"]
            salt_per100 = [float(ing[i].get("sodium_per_100g_mg") or SALT_SODIUM_MG_PER_100G) for i in blank_salt]
            oil_rows = [
                {
                    "kind": _oil_kind(entry_by_name.get(names2[i].lower(), {})),
                    "kcal": float(ing[i].get("energy_kcal_per_100g") or OIL_KCAL_PER_100G),
                    "fat": float(ing[i].get("fat_per_100g") or 100.0),
                }
                for i in blank_oil
            ]
            rows.append({
                "dataset": dataset, "title": recipe["title"], "serves": serves,
                "truth": {"sodium": sodium_t, "kcal": kcal_t, "fat": fat_t},
                "base": base, "n_salt": len(blank_salt), "salt_per100": salt_per100, "oil": oil_rows,
            })
            done += 1
            if args.limit and done >= args.limit:
                break
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"recipes_{args.source}.json").write_text(json.dumps(rows, indent=1))
    report(rows, args.source)


def report(rows: list[dict], source: str) -> None:
    lines = [f"# Salt / oil policy evaluation (source={source})", f"recipes profiled: {len(rows)}", ""]
    for dataset in ("safefood", "healthyfoods"):
        sub = [r for r in rows if r["dataset"] == dataset and r["n_salt"] and r["truth"]["sodium"]]
        lines.append(f"## Salt, {dataset}: {len(sub)} recipes with blank salt and sodium truth")
        for grams in SALT_G:
            errs = [
                r["base"]["sodium"] + sum(grams * p / 100.0 for p in r["salt_per100"]) - r["truth"]["sodium"]
                for r in sub
            ]
            if errs:
                lines.append(f"- {grams:>4} g per blank salt row: MAE {statistics.mean(map(abs, errs)):.1f} mg, bias {statistics.mean(errs):+.1f} mg/serving")
        lines.append("")
    for kind, grid in (("grease", GREASE_G), ("fry", FRY_G), ("other", FRY_G)):
        for dataset in ("safefood", "healthyfoods"):
            sub = [r for r in rows if r["dataset"] == dataset and any(o["kind"] == kind for o in r["oil"]) and r["truth"]["kcal"]]
            lines.append(f"## Oil ({kind}), {dataset}: {len(sub)} recipes with kcal truth")
            for grams in grid:
                errs = [
                    r["base"]["kcal"]
                    + sum(grams * o["kcal"] / 100.0 for o in r["oil"] if o["kind"] == kind) / r["serves"]
                    - r["truth"]["kcal"]
                    for r in sub
                ]
                if errs:
                    lines.append(f"- {grams:>4} g per blank oil row: MAE {statistics.mean(map(abs, errs)):.1f} kcal, bias {statistics.mean(errs):+.1f} kcal/serving")
            lines.append("")
    (OUT / f"report_{source}.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
