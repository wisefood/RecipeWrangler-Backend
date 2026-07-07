#!/usr/bin/env python3
"""Post-retag sanity check on recomputed recipe profiles.

Reads a JSONL of profiled-recipe dicts (one per line — the natural retag output;
each dict is roughly what ``Recipe_Profiling_Chain_Structured`` returns) and
reports: the per-serving-kcal distribution, the ``serves_source`` /
``weights_capped`` / ``nutrition_coverage`` / ``match_confidence`` mix, recipes
with implausible totals, and — where a recipe carries source-provided ("ground
truth") nutrition — the divergence between that and the recomputed values.

    python3 scripts/validate_profiling.py --input profiled.jsonl
    python3 scripts/validate_profiling.py --input profiled.jsonl --report-csv flagged.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# kcal/serving buckets — the <50 and >1200 ones are the suspicious tails.
_KCAL_BUCKETS = [(0, 50), (50, 200), (200, 400), (400, 700), (700, 1200), (1200, 1e9)]
_DIVERGENCE_PCT = 25.0  # flag a recipe where computed vs source differs by more than this


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_path(d: dict, *keys):
    """Return the first non-None value found at any of the given dotted/flat keys."""
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _kcal_per_serving(rec: dict):
    serves = _f(_get_path(rec, "serves", "profiling_quality.serves", "full_profile.nutrition_summary.serves")) or 1.0
    # try a per-serving key first, then a total / serves
    for k in list(rec.keys()):
        if k.startswith("total_energy_kcal_per_serving"):
            v = _f(rec[k])
            if v is not None:
                return v
    ns = _get_path(rec, "full_profile.nutrition_summary") or {}
    v = _f(ns.get("energy_kcal_per_serving"))
    if v is not None:
        return v
    total = _f(_get_path(rec, "profiling_totals.clean_totals.energy_kcal", "total_energy_kcal", "full_profile.nutrition_summary.energy_kcal"))
    if total is None:
        # last resort: scan profiling_totals for a total_energy_kcal_* key
        pt = _get_path(rec, "profiling_totals") or {}
        for k, vv in pt.items():
            if k.startswith("total_energy_kcal") and "per_serving" not in k:
                total = _f(vv)
                break
    return None if total is None or serves <= 0 else total / serves


def _source_nutrition(rec: dict):
    """{kcal, protein_g, carbohydrate_g, fat_g} per serving from source-provided
    ('ground truth') values, if the recipe carries any. None otherwise."""
    gt = _get_path(rec, "ground_truth_nutrition", "source_nutrition", "source_provided_nutrition")
    if not isinstance(gt, dict):
        return None
    out = {
        "kcal": _f(gt.get("energy_kcal") or gt.get("kcal") or gt.get("calories")),
        "protein_g": _f(gt.get("protein_g") or gt.get("protein")),
        "carbohydrate_g": _f(gt.get("carbohydrate_g") or gt.get("carbs_g") or gt.get("carbohydrate")),
        "fat_g": _f(gt.get("fat_g") or gt.get("fat")),
    }
    return out if any(v is not None for v in out.values()) else None


def _computed_per_serving(rec: dict):
    ns = _get_path(rec, "full_profile.nutrition_summary") or {}
    return {
        "kcal": _kcal_per_serving(rec),
        "protein_g": _f(ns.get("protein_g_per_serving")),
        "carbohydrate_g": _f(ns.get("carbohydrate_g_per_serving")),
        "fat_g": _f(ns.get("fat_g_per_serving")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL of profiled-recipe dicts")
    parser.add_argument("--report-csv", type=Path, default=None, help="write flagged recipes here")
    parser.add_argument("--low-coverage", type=float, default=0.80)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    n = 0
    kcal_hist = Counter()
    serves_src = Counter()
    capped = 0
    cov_buckets = Counter()  # 0-0.5, 0.5-0.8, 0.8-1.0
    conf_mix = Counter()     # over all ingredient match_confidence values seen
    flagged: list[dict] = []
    gt_n = 0
    gt_div = []  # per-recipe max |computed-source|/source over the 4 macros

    with args.input.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            n += 1
            if args.limit and n > args.limit:
                n -= 1
                break
            title = str(rec.get("title") or rec.get("recipe_id") or f"#{n}")

            kpc = _kcal_per_serving(rec)
            if kpc is not None:
                for lo, hi in _KCAL_BUCKETS:
                    if lo <= kpc < hi:
                        kcal_hist[f"{lo}-{int(hi) if hi < 1e9 else 'inf'}"] += 1
                        break

            ss = _get_path(rec, "serves_source", "profiling_quality.serves_source", "pipeline_trace.profiling.quality.serves_source")
            serves_src[str(ss or "unknown")] += 1

            wc = _get_path(rec, "weights_capped", "profiling_quality.weights_capped", "pipeline_trace.profiling.quality.weights_capped")
            if wc:
                capped += 1

            cov = _f(_get_path(rec, "nutrition_coverage", "profiling_quality.nutrition_coverage", "pipeline_trace.profiling.quality.nutrition_coverage"))
            if cov is not None:
                cov_buckets["<0.5" if cov < 0.5 else ("0.5-0.8" if cov < 0.8 else ">=0.8")] += 1

            for ing in (rec.get("ingredients") or rec.get("full_profile", {}).get("ingredients") or []):
                if isinstance(ing, dict):
                    conf_mix[str(ing.get("match_confidence") or "?")] += 1

            reasons = []
            if kpc is not None and (kpc < 30 or kpc > 1500):
                reasons.append(f"kcal/serving={kpc:.0f}")
            if cov is not None and cov < args.low_coverage:
                reasons.append(f"coverage={cov:.2f}")
            if wc:
                reasons.append("weights_capped")
            src = _source_nutrition(rec)
            if src:
                gt_n += 1
                comp = _computed_per_serving(rec)
                worst = 0.0
                for k in ("kcal", "protein_g", "carbohydrate_g", "fat_g"):
                    a, b = comp.get(k), src.get(k)
                    if a is not None and b is not None and b > 1e-6:
                        worst = max(worst, abs(a - b) / b)
                gt_div.append(worst)
                if worst * 100.0 > _DIVERGENCE_PCT:
                    reasons.append(f"src_divergence={worst*100:.0f}%")
            if reasons:
                flagged.append({"title": title, "kcal_per_serving": "" if kpc is None else round(kpc, 1),
                                "coverage": "" if cov is None else cov, "serves_source": ss or "",
                                "weights_capped": bool(wc), "reasons": "; ".join(reasons)})

    print(f"recipes: {n}")
    print(f"\nkcal/serving distribution: {dict(kcal_hist)}")
    susp = kcal_hist.get("0-50", 0) + kcal_hist.get("1200-inf", 0)
    print(f"  suspicious tails (<50 or >1200 kcal/serving): {susp} ({100*susp/max(1,n):.1f}%)")
    print(f"serves_source: {dict(serves_src)}")
    print(f"recipes with capped weights: {capped} ({100*capped/max(1,n):.1f}%)")
    print(f"nutrition coverage buckets: {dict(cov_buckets)}")
    print(f"ingredient match_confidence mix: {dict(conf_mix)}")
    if gt_n:
        gt_div.sort()
        m = gt_div[len(gt_div) // 2]
        over = sum(1 for d in gt_div if d * 100 > _DIVERGENCE_PCT)
        print(f"\nsource-provided nutrition cross-check: {gt_n} recipes; median worst-macro divergence {m*100:.1f}%; "
              f"{over} ({100*over/max(1,gt_n):.1f}%) over {_DIVERGENCE_PCT:.0f}%")
    print(f"\nflagged recipes: {len(flagged)} ({100*len(flagged)/max(1,n):.1f}%)")
    for r in flagged[:30]:
        print(f"  {r['title'][:50]:<52} | {r['reasons']}")
    if args.report_csv and flagged:
        args.report_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.report_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["title", "kcal_per_serving", "coverage", "serves_source", "weights_capped", "reasons"])
            w.writeheader()
            w.writerows(flagged)
        print(f"\nwrote {args.report_csv}")


if __name__ == "__main__":
    main()
