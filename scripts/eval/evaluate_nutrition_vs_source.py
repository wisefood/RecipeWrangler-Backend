#!/usr/bin/env python3
"""Compare our computed (EU-region) nutrition against each dataset's own published
reference nutrition, for every dataset that actually has one.

Four datasets have an independent reference to compare against:
  - Curated Irish Recipes      (nutrition_source='safefood_rcsi', SafeFood lab values)
  - MyPlate                    (nutrition_source='myplate', scraped from myplate.food)
  - Curated Hungarian Recipes  (nutrition_source='planeat', direct CoFID computation)
  - Curated Slovenian Recipes  (nutrition_source='slovenian_original', direct OPKP computation)

Everything else (FoodHero, Best of Hungary, The Hungary Soul, Irish Heart
Foundation, SuperValu, Slovenian Kitchen) has no independent reference
nutrition anywhere -- their only nutrition values ARE the generated ones, so
there's nothing to diff them against. Excluded, not because they're wrong,
but because "vs source" doesn't apply to them (noted in the top-level README).

HealthyFoods is included -- its reparse finished. It has no reference nutrition_source
row in Postgres (unlike the other 4 datasets), so its truth is read directly from the
raw scrape (data/HealthyFoods/HealthyFood_recipes_nutrition_clean.json) and joined to
the computed side by title.

Reads live Postgres. Writes one subfolder per dataset, each with a CSV of
every recipe/nutrient pair, a CSV of the most-deviated recipes, a CSV of
per-nutrient outliers, a CSV of Nutri-Score grade mismatches, a summary
markdown, and three charts. Plus a top-level README indexing all of it.

Usage: PYTHONPATH=src .venv/bin/python scripts/eval/evaluate_nutrition_vs_source.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data/eval/nutrition_vs_source"

# wisefood palette, sampled from Figure_17_nutrient_medians_irish_curated.png
WISEFOOD_SOURCE = "#5F2C77"   # Reference purple
WISEFOOD_CALC = "#D53F60"     # Hungarian pink
WISEFOOD_BAR3 = "#94B236"     # Irish green (top-deviated chart)
GRAM_NUTRIENTS = ["protein_g", "carbohydrate_g", "fat_g", "saturated_fat_g", "sugar_g", "fibre_g"]

NUTRIENTS = [
    "energy_kcal", "protein_g", "carbohydrate_g", "fat_g",
    "saturated_fat_g", "sugar_g", "fibre_g", "sodium_mg",
]
NUTRIENT_LABEL = {
    "energy_kcal": "Energy (kcal)", "protein_g": "Protein (g)", "carbohydrate_g": "Carbs (g)",
    "fat_g": "Fat (g)", "saturated_fat_g": "Saturated fat (g)", "sugar_g": "Sugar (g)",
    "fibre_g": "Fibre (g)", "sodium_mg": "Sodium (mg)",
}
# (display label, db source, reference nutrition_source, generated nutrition_source)
# Computed region matches the dataset's own country wherever this pipeline has one:
# Irish -> irish, Hungarian -> hungarian, Slovenian -> slovenian. MyPlate has no US
# region in this pipeline (the 4 generated regions are IE/HU/EU/SI, see CLAUDE.md) --
# 'eu' is used as the least-wrong available default and called out in the README as
# NOT actually region-matched, unlike the other three.
DATASETS = [
    ("Curated Irish Recipes (RCSI lab)", "Curated Irish Recipes", "safefood_rcsi", "irish"),
    ("Curated Irish Recipes (SafeFood web)", "Curated Irish Recipes", "safefood_web", "irish"),
    ("MyPlate", "MyPlate", "myplate", "eu"),
    ("Curated Hungarian Recipes", "Curated Hungarian Recipes", "planeat", "hungarian"),
    ("Curated Slovenian Recipes", "Curated Slovenian Recipes", "slovenian_original", "slovenian"),
]
NOT_REGION_MATCHED = {"MyPlate"}
NO_REFERENCE = [
    "FoodHero", "Best of Hungary", "The Hungary Soul",
    "Irish Heart Foundation", "SuperValu", "Slovenian Kitchen",
]
# Flat gram floor is meaningless for sodium_mg (dominated the old "worst" ranking on
# unit-scale noise, not real bugs) -- per-nutrient floors instead.
MIN_TRUTH_BY_NUTRIENT = {
    "energy_kcal": 5.0, "protein_g": 0.5, "carbohydrate_g": 0.5, "fat_g": 0.5,
    "saturated_fat_g": 0.5, "sugar_g": 0.5, "fibre_g": 0.5, "sodium_mg": 20.0,
}
# A single-serving dish outside these bounds is worth a human look regardless of whether
# the other side agrees (a %-error ranking alone misses "both sides say 3000kcal"). Most
# nutrients have no meaningful LOW bound (0g fat in a fruit salad is normal), so only kcal
# flags implausibly-low; the rest are upper-bound-only, generous single-serving ceilings.
NUTRIENT_BOUNDS = {
    "energy_kcal": (20.0, 1200.0),
    "protein_g": (0.0, 150.0),
    "carbohydrate_g": (0.0, 200.0),
    "fat_g": (0.0, 120.0),
    "saturated_fat_g": (0.0, 60.0),
    "sugar_g": (0.0, 150.0),
    "fibre_g": (0.0, 50.0),
    "sodium_mg": (0.0, 4000.0),
}
KCAL_IMPLAUSIBLE_LOW, KCAL_IMPLAUSIBLE_HIGH = NUTRIENT_BOUNDS["energy_kcal"]
# A collapse to near-zero on one side while the other is normal (e.g. the dominant
# ingredient went unmatched) won't cross the absolute NUTRIENT_BOUNDS ceiling -- it needs
# its own check: a big ratio gap AND an absolute gap worth caring about.
RATIO_GAP_ABS_FLOOR = {
    "energy_kcal": 50.0, "protein_g": 8.0, "carbohydrate_g": 15.0, "fat_g": 8.0,
    "saturated_fat_g": 4.0, "sugar_g": 10.0, "fibre_g": 4.0, "sodium_mg": 150.0,
}
NUTRI_SCORE_GRADES = ["A", "B", "C", "D", "E"]
NUTRI_SCORE_COLOR = {"A": "#1E7A34", "B": "#7CB518", "C": "#F4C430", "D": "#E8781E", "E": "#C0392B"}


def _nutri_grade(value) -> str | None:
    if not value:
        return None
    s = value.get("nutri_score") if isinstance(value, dict) else None
    if not s:
        return None
    m = re.search(r"([A-E])\s*$", str(s), re.I)
    return m.group(1).upper() if m else None


def _connect():
    return psycopg2.connect(
        host=os.getenv("NUTRITION_HOST", "localhost"),
        port=os.getenv("NUTRITION_PORT", "5432"),
        dbname=os.getenv("NUTRITION_DB", "nutrients"),
        user=os.getenv("NUTRITION_USER", "postgres"),
        password=os.getenv("NUTRITION_PASSWORD", "postgres"),
    )


# Some reference sources spell keys differently than our own schema.
_KEY_ALIASES = {"carbs_g": "carbohydrate_g"}


def _fetch(cur, source: str, nutrition_source: str) -> dict[str, dict]:
    cur.execute(
        'SELECT recipe_id, title, total_nutrients_per_serving, pipeline_version, nutri_score FROM "nutrients-recipe-profiles" '
        "WHERE source = %s AND nutrition_source = %s AND total_nutrients_per_serving IS NOT NULL",
        (source, nutrition_source),
    )
    rows = {}
    for row in cur.fetchall():
        n = row["total_nutrients_per_serving"] or {}
        for old, new in _KEY_ALIASES.items():
            if old in n and new not in n:
                n[new] = n[old]
        rows[row["recipe_id"]] = row
    return rows


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _healthyfoods_truth() -> dict[str, dict]:
    """HealthyFoods has no reference nutrition_source row in Postgres (unlike the
    other 4 datasets) -- its published nutrition lives only in the raw scrape,
    title-keyed. Returns the same shape _fetch() would, keyed by title (joined to
    a real recipe_id by the caller, since this source has none)."""
    key_map = {"Calories": "energy_kcal", "Total fat": "fat_g", "Saturated fat": "saturated_fat_g",
               "Sugar": "sugar_g", "Sodium": "sodium_mg", "Protein": "protein_g",
               "Carbohydrates": "carbohydrate_g", "Dietary fibre": "fibre_g"}
    path = REPO_ROOT / "data/HealthyFoods/HealthyFood_recipes_nutrition_clean.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for rec in data.get("recipes", []):
        title = rec.get("title")
        n = rec.get("nutrition_per_serve") or {}
        nutrients = {}
        for old, new in key_map.items():
            v = n.get(old)
            if v is None:
                continue
            m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v))  # "298 cal", "6.9 g", "670 mg" -> leading number
            if m:
                nutrients[new] = float(m.group(1))
        if title and nutrients:
            out[title] = {"total_nutrients_per_serving": nutrients, "title": title,
                          "pipeline_version": "healthyfoods_scraped_reference", "nutri_score": None}
    return out


def _run_dataset(cur, out: Path, label: str, db_source: str, ref_source: str, calc_source: str,
                  truth_override: dict[str, dict] | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    if truth_override is not None:
        # Truth is title-keyed (see _healthyfoods_truth); re-key to calc's recipe_id by title.
        calc = _fetch(cur, db_source, calc_source)
        by_title = {c["title"]: rid for rid, c in calc.items()}
        truth = {}
        for title, row in truth_override.items():
            rid = by_title.get(title)
            if rid:
                truth[rid] = row
    else:
        truth = _fetch(cur, db_source, ref_source)
        calc = _fetch(cur, db_source, calc_source)
    common = sorted(set(truth) & set(calc))
    calc_versions = sorted({calc[rid]["pipeline_version"] for rid in common if calc[rid].get("pipeline_version")})

    detail_rows = []
    recipe_rows = []
    grade_rows = []
    for rid in common:
        t = truth[rid]["total_nutrients_per_serving"] or {}
        c = calc[rid]["total_nutrients_per_serving"] or {}
        title = truth[rid]["title"] or calc[rid]["title"]
        tg, cg = _nutri_grade(truth[rid].get("nutri_score")), _nutri_grade(calc[rid].get("nutri_score"))
        if tg and cg:
            grade_rows.append({"recipe_id": rid, "title": title, "truth_grade": tg, "calc_grade": cg,
                                "match": tg == cg})
        pct_errors = []
        for nut in NUTRIENTS:
            tv, cv = t.get(nut), c.get(nut)
            diff = None if (tv is None or cv is None) else cv - tv
            pct = None
            if tv is not None and cv is not None and abs(tv) >= MIN_TRUTH_BY_NUTRIENT[nut]:
                pct = (cv - tv) / tv * 100.0
                pct_errors.append(abs(pct))
            detail_rows.append({
                "recipe_id": rid, "title": title, "nutrient": nut,
                "truth": tv, "calc": cv, "diff": diff, "pct_error": pct,
            })
        if pct_errors:
            recipe_rows.append({
                "recipe_id": rid, "title": title,
                "mean_abs_pct_error": statistics.mean(pct_errors),
                "n_nutrients": len(pct_errors),
            })

    # --- deviation_details.csv ---
    with open(out / "deviation_details.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["recipe_id", "title", "nutrient", "truth", "calc", "diff", "pct_error"])
        w.writeheader()
        w.writerows(detail_rows)

    # --- most_deviated.csv (top 30, with per-nutrient breakdown columns) ---
    recipe_rows.sort(key=lambda r: r["mean_abs_pct_error"], reverse=True)
    top = recipe_rows[:30]
    by_nutrient = {(r["recipe_id"], r["nutrient"]): r for r in detail_rows}
    with open(out / "most_deviated.csv", "w", newline="") as f:
        fieldnames = ["recipe_id", "title", "mean_abs_pct_error"] + [f"{n}_pct_error" for n in NUTRIENTS] + [f"{n}_truth" for n in NUTRIENTS] + [f"{n}_calc" for n in NUTRIENTS]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in top:
            row = {"recipe_id": r["recipe_id"], "title": r["title"],
                   "mean_abs_pct_error": round(r["mean_abs_pct_error"], 1)}
            for nut in NUTRIENTS:
                d = by_nutrient.get((r["recipe_id"], nut))
                row[f"{nut}_pct_error"] = None if not d or d["pct_error"] is None else round(d["pct_error"], 1)
                row[f"{nut}_truth"] = d["truth"] if d else None
                row[f"{nut}_calc"] = None if not d else (round(d["calc"], 1) if d["calc"] is not None else None)
            w.writerow(row)

    # --- summary.md ---
    lines = [f"# {label}: computed vs. source-reference nutrition", "",
             f"{len(recipe_rows)} recipes with both a reference (`{ref_source}`) and a computed "
             f"(`{calc_source}`) profile. Computed pipeline version(s): {', '.join(calc_versions) or 'unknown'}.", "",
             "| Nutrient | n | median abs % error | mean abs % error | median calc/truth |",
             "|---|---:|---:|---:|---:|"]
    nutrient_medians = {}
    for nut in NUTRIENTS:
        pairs = [(d["calc"], d["truth"]) for d in detail_rows
                 if d["nutrient"] == nut and d["truth"] is not None and abs(d["truth"]) >= MIN_TRUTH_BY_NUTRIENT[nut] and d["calc"] is not None]
        if not pairs:
            continue
        abs_pct = [abs(c - t) / t * 100.0 for c, t in pairs]
        ratio = [c / t for c, t in pairs]
        nutrient_medians[nut] = statistics.median(abs_pct)
        lines.append(
            f"| {NUTRIENT_LABEL[nut]} | {len(pairs)} | {statistics.median(abs_pct):.0f}% | "
            f"{statistics.mean(abs_pct):.0f}% | {statistics.median(ratio):.2f} |"
        )
    lines.append("")
    lines.append("Top 5 most-deviated recipes (see `most_deviated.csv` for all 30 with the per-nutrient breakdown):")
    lines.append("")
    for r in top[:5]:
        lines.append(f"- **{r['title']}** ({r['recipe_id']}) -- mean abs % error {r['mean_abs_pct_error']:.0f}%")
    (out / "summary.md").write_text("\n".join(lines))

    def _median_pair(nut: str) -> tuple[float, float] | None:
        truths = [d["truth"] for d in detail_rows if d["nutrient"] == nut and d["truth"] is not None]
        calcs = [d["calc"] for d in detail_rows if d["nutrient"] == nut and d["calc"] is not None]
        if not truths or not calcs:
            return None
        return statistics.median(truths), statistics.median(calcs)

    def _grouped_bars(ax, nuts, title, ylabel):
        pairs = [(_median_pair(n) or (0, 0)) for n in nuts]
        x = range(len(nuts))
        width = 0.35
        truth_bars = ax.bar([xi - width / 2 for xi in x], [p[0] for p in pairs], width, color=WISEFOOD_SOURCE, label="Source")
        calc_bars = ax.bar([xi + width / 2 for xi in x], [p[1] for p in pairs], width, color=WISEFOOD_CALC, label="Calculated")
        for bars in (truth_bars, calc_bars):
            ax.bar_label(bars, fmt="%.1f", fontsize=8, padding=2)
        ax.set_xticks(list(x))
        ax.set_xticklabels([NUTRIENT_LABEL[n].split(" (")[0] for n in nuts])
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.set_axisbelow(True)

    def _nutri_score_panel(ax):
        if not grade_rows:
            ax.axis("off")
            ax.set_title("D. Nutri-Score (no grade data)", fontweight="bold")
            return
        truth_counts = [sum(1 for r in grade_rows if r["truth_grade"] == g) for g in NUTRI_SCORE_GRADES]
        calc_counts = [sum(1 for r in grade_rows if r["calc_grade"] == g) for g in NUTRI_SCORE_GRADES]
        x = range(len(NUTRI_SCORE_GRADES))
        width = 0.35
        ax.bar([xi - width / 2 for xi in x], truth_counts, width, color=WISEFOOD_SOURCE, label="Source")
        ax.bar([xi + width / 2 for xi in x], calc_counts, width, color=WISEFOOD_CALC, label="Calculated")
        ax.set_xticks(list(x))
        ax.set_xticklabels(NUTRI_SCORE_GRADES)
        for xi, g in zip(x, NUTRI_SCORE_GRADES):
            ax.get_xticklabels()[xi].set_color(NUTRI_SCORE_COLOR[g])
            ax.get_xticklabels()[xi].set_fontweight("bold")
        agree = sum(1 for r in grade_rows if r["match"]) / len(grade_rows) * 100.0
        ax.set_ylabel("Recipes")
        ax.set_title(f"D. Nutri-Score\n{agree:.0f}% exact grade agreement (n={len(grade_rows)})", fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.set_axisbelow(True)

    # --- chart: source vs calculated nutrient medians + Nutri-Score (4 panels, matching Figure 17 style) ---
    fig, (ax_a, ax_b, ax_c, ax_d) = plt.subplots(1, 4, figsize=(22, 6), gridspec_kw={"width_ratios": [3, 1, 1, 1.3]})
    gram_nuts = [n for n in GRAM_NUTRIENTS if n in nutrient_medians]
    _grouped_bars(ax_a, gram_nuts, "A. Gram-based nutrients", "Median per serving (g)")
    _grouped_bars(ax_b, ["energy_kcal"], "B. Energy", "Median per serving (kcal)")
    _grouped_bars(ax_c, ["sodium_mg"], "C. Sodium", "Median per serving (mg)")
    _nutri_score_panel(ax_d)
    handles, labels = ax_a.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.02), frameon=True)
    fig.suptitle(f"{label}: source vs. calculated nutrient medians\nPer-serving basis", fontsize=15, fontweight="bold")
    fig.tight_layout(rect=[0, 0.04, 1, 0.92])
    fig.savefig(out / "source_vs_calculated_medians.png", dpi=150)
    plt.close(fig)

    # --- chart: top 15 most-deviated recipes ---
    fig, ax = plt.subplots(figsize=(9, 6))
    top15 = recipe_rows[:15][::-1]
    labels = [r["title"][:40] for r in top15]
    ax.barh(labels, [r["mean_abs_pct_error"] for r in top15], color=WISEFOOD_CALC)
    ax.set_xlabel("Mean absolute % error across nutrients")
    ax.set_title(f"{label} -- most-deviated recipes", fontweight="bold")
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out / "top_deviated.png", dpi=150)
    plt.close(fig)

    # --- outliers: recipes with an implausible value on either side, for EVERY nutrient, not
    # just a big % gap -- catches "a LOT of calories/protein/sodium that doesn't make sense"
    # even when the OTHER side agrees with it (a %-error ranking alone would miss that) ---
    by_nutrient_recipe: dict[str, dict[str, dict]] = {nut: {} for nut in NUTRIENTS}
    for d in detail_rows:
        by_nutrient_recipe[d["nutrient"]][d["recipe_id"]] = d

    outliers = []
    for nut in NUTRIENTS:
        low, high = NUTRIENT_BOUNDS[nut]
        gap_floor = RATIO_GAP_ABS_FLOOR[nut]
        for rid, d in by_nutrient_recipe[nut].items():
            reasons = []
            if d["truth"] is not None and d["truth"] > high:
                reasons.append(f"source {NUTRIENT_LABEL[nut]} implausibly high ({d['truth']:.0f})")
            if d["truth"] is not None and 0 < d["truth"] < low:
                reasons.append(f"source {NUTRIENT_LABEL[nut]} implausibly low ({d['truth']:.0f})")
            if d["calc"] is not None and d["calc"] > high:
                reasons.append(f"calculated {NUTRIENT_LABEL[nut]} implausibly high ({d['calc']:.0f})")
            if d["calc"] is not None and 0 < d["calc"] < low:
                reasons.append(f"calculated {NUTRIENT_LABEL[nut]} implausibly low ({d['calc']:.0f})")
            # collapse check: one side near-zero, other side normal -- e.g. the recipe's
            # dominant ingredient went unmatched. Ratio bounds alone miss this because
            # neither absolute value needs to cross NUTRIENT_BOUNDS for it to be a bug.
            tv, cv = d["truth"], d["calc"]
            if tv is not None and cv is not None and abs(tv - cv) >= gap_floor:
                lo_val, hi_val = min(tv, cv), max(tv, cv)
                if lo_val <= 0 or hi_val / max(lo_val, 1e-6) >= 3.0:
                    which = "calculated" if cv < tv else "source"
                    reasons.append(
                        f"{which} {NUTRIENT_LABEL[nut]} collapsed relative to the other side "
                        f"(source={tv:.0f}, calc={cv:.0f})"
                    )
            if reasons:
                outliers.append({"recipe_id": rid, "title": d["title"], "nutrient": nut,
                                  "truth": d["truth"], "calc": d["calc"], "reasons": "; ".join(reasons)})
    outliers.sort(key=lambda o: (o["nutrient"] != "energy_kcal", -(max(o["truth"] or 0, o["calc"] or 0))))
    with open(out / "outliers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["recipe_id", "title", "nutrient", "truth", "calc", "reasons"])
        w.writeheader()
        w.writerows(outliers)
    outlier_recipe_ids_by_nutrient = {
        nut: {o["recipe_id"] for o in outliers if o["nutrient"] == nut} for nut in NUTRIENTS
    }

    # --- chart: per-recipe value, source vs calculated, outliers marked -- one small panel
    # per nutrient so every nutrient gets the same outlier view kcal got before ---
    fig, axes = plt.subplots(2, 4, figsize=(20, 9))
    for ax, nut in zip(axes.flat, NUTRIENTS):
        low, high = NUTRIENT_BOUNDS[nut]
        vals = list(by_nutrient_recipe[nut].values())
        vals.sort(key=lambda d: d["calc"] if d["calc"] is not None else -1)
        x = range(len(vals))
        ax.scatter(x, [d["truth"] for d in vals], s=6, alpha=0.5, color=WISEFOOD_SOURCE, label="Source")
        ax.scatter(x, [d["calc"] for d in vals], s=6, alpha=0.5, color=WISEFOOD_CALC, label="Calculated")
        outlier_ids = outlier_recipe_ids_by_nutrient[nut]
        ox = [i for i, d in enumerate(vals) if d["recipe_id"] in outlier_ids]
        oy = [max(d["truth"] or 0, d["calc"] or 0) for d in vals if d["recipe_id"] in outlier_ids]
        if ox:
            ax.scatter(ox, oy, s=35, facecolors="none", edgecolors="black", linewidths=1.1, zorder=5)
        ax.axhline(high, color="black", linestyle=":", linewidth=0.8, alpha=0.6)
        if low > 0:
            ax.axhline(low, color="black", linestyle=":", linewidth=0.8, alpha=0.6)
        ax.set_title(f"{NUTRIENT_LABEL[nut]} ({len(ox)} outliers)", fontsize=10, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.3)
        ax.set_axisbelow(True)
    handles, labels_ = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels_, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.02), frameon=True)
    fig.suptitle(f"{label} -- per-recipe values, source vs. calculated, per nutrient\n"
                 "dotted lines mark implausible single-serving bounds; circled points are outliers",
                 fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0.05, 1, 0.93])
    fig.savefig(out / "nutrient_outliers.png", dpi=150)
    plt.close(fig)

    # --- Nutri-Score: source vs calculated grade agreement ---
    grade_agreement = None
    if grade_rows:
        truth_counts = {g: sum(1 for r in grade_rows if r["truth_grade"] == g) for g in NUTRI_SCORE_GRADES}
        calc_counts = {g: sum(1 for r in grade_rows if r["calc_grade"] == g) for g in NUTRI_SCORE_GRADES}
        n_match = sum(1 for r in grade_rows if r["match"])
        grade_agreement = n_match / len(grade_rows) * 100.0
        with open(out / "nutri_score_mismatches.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["recipe_id", "title", "truth_grade", "calc_grade"])
            w.writeheader()
            w.writerows({k: r[k] for k in ("recipe_id", "title", "truth_grade", "calc_grade")}
                        for r in grade_rows if not r["match"])

    return {"label": label, "ref_source": ref_source, "calc_source": calc_source, "n_outliers": len(outliers),
            "n_recipes": len(recipe_rows), "nutrient_medians": nutrient_medians,
            "calc_versions": calc_versions, "grade_agreement": grade_agreement,
            "grade_counts": (truth_counts, calc_counts) if grade_rows else None,
            "n_graded": len(grade_rows)}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    results = []
    for label, db_source, ref_source, calc_source in DATASETS:
        out = OUT_DIR / _slug(label)
        results.append(_run_dataset(cur, out, label, db_source, ref_source, calc_source))
        print(f"{label}: {results[-1]['n_recipes']} recipes -> {out}")

    # HealthyFoods: no reference nutrition_source row in Postgres (unlike the other 4),
    # truth comes from the raw scrape instead, joined to calc by title.
    hf_label = "HealthyFoods"
    out = OUT_DIR / _slug(hf_label)
    results.append(_run_dataset(cur, out, hf_label, "HealthyFoods", "healthyfoods_scraped",
                                 "eu", truth_override=_healthyfoods_truth()))
    print(f"{hf_label}: {results[-1]['n_recipes']} recipes -> {out}")

    conn.close()

    # --- top-level README ---
    lines = ["# Nutrition vs. source-reference deviation", "",
             "Computed nutrition vs each dataset's own published/direct-computed reference. "
             "One subfolder per dataset: `deviation_details.csv` (every recipe x nutrient), "
             "`most_deviated.csv` (top 30 worst recipes with per-nutrient breakdown), "
             "`outliers.csv` (recipes with an implausible per-serving value on EITHER side, for "
             "every nutrient -- not just kcal -- flagged regardless of whether the two sides agree "
             "with each other; see `NUTRIENT_BOUNDS` in the script for the per-nutrient thresholds), "
             "`nutri_score_mismatches.csv` (recipes where source and calculated Nutri-Score grades "
             "disagree), `summary.md`, and three charts (`source_vs_calculated_medians.png` -- now "
             "4 panels, D is Nutri-Score grade counts; `top_deviated.png`; `nutrient_outliers.png` "
             "-- one panel per nutrient).",
             "", "## Datasets evaluated", "",
             "Computed region is region-matched to the dataset's own country wherever this "
             "pipeline has one (Irish -> `irish`, Hungarian -> `hungarian`, Slovenian -> "
             "`slovenian`). **MyPlate is the exception** -- this pipeline only generates "
             "IE/HU/EU/SI regions, there is no US region, so `eu` is used as the least-wrong "
             "available default. MyPlate's numbers are not truly region-matched; read them "
             "knowing that.", "",
             "| Dataset | Folder | Recipes | Reference | Computed region | Pipeline version | Median kcal error | Outliers | Nutri-Score agreement |",
             "|---|---|---:|---|---|---|---:|---:|---:|"]
    for r in results:
        kcal = r["nutrient_medians"].get("energy_kcal")
        kcal_s = f"{kcal:.0f}%" if kcal is not None else "n/a"
        versions = ", ".join(r["calc_versions"]) or "unknown"
        grade_s = f"{r['grade_agreement']:.0f}% (n={r['n_graded']})" if r["grade_agreement"] is not None else "n/a"
        lines.append(f"| {r['label']} | `{_slug(r['label'])}/` | {r['n_recipes']} | `{r['ref_source']}` | `{r['calc_source']}` | {versions} | {kcal_s} | {r['n_outliers']} | {grade_s} |")
    lines += ["", "## Datasets with no independent reference (not evaluated)", "",
              "These datasets have no published or independently-computed nutrition to diff against -- "
              "their only nutrition values in Postgres ARE the ones this pipeline generated, so "
              '"vs source" does not apply to them:', ""]
    for name in NO_REFERENCE:
        lines.append(f"- {name}")
    lines += ["", "HealthyFoods now included (its reparse finished) -- truth from the raw scrape "
              "(no reference nutrition_source row exists for it in Postgres, unlike the other 4).", "",
              "## Known reference-data quality issues (not our pipeline)", "",
              "Investigating the worst SafeFood-referenced deviations found published values on "
              "SafeFood's own site that are physically inconsistent with their own stated ingredient "
              "list, not an error in our computation:", "",
              "- **Green risotto** (safefood.ie): ingredient list includes \"4 teaspoons of olive "
              "oil\" for 2 servings (~9g fat/serving from oil alone). Published label: `fat_g: 3.1g`.",
              "- **Cod tray bake** (safefood.ie): \"2 tablespoons olive oil\" for 4 servings "
              "(~6g fat/serving from oil alone). Published label: `fat_g: 1.4g`.",
              "- Cross-checked against a near-identical sister recipe on the same site (\"Pea "
              "risotto\", similar oil-per-serving), which reports a plausible `fat_g: 11g` -- these "
              "two look like isolated publishing errors on SafeFood's side, not a systematic flaw "
              "in their data. Their `fat_g`/`sodium_mg` numbers are used as our ground truth for "
              "this eval, so these specific recipes' deviation numbers should be read with that in "
              "mind rather than as a pipeline defect.", "",
              "## Fixed this pass (see HANDOFF_PARSING_MATCHING.md for full detail)", "",
              "- `cucumber` and `brewed coffee` were matching the wrong sense of the word in the "
              "composition database (a bread-spread product, and dry coffee grounds).",
              "- Curated Hungarian/Slovenian Recipes had no ingredient weights in the graph at all "
              "for most recipes -- backfilled from their own raw sources.",
              "- 9 of 10 recipes where a stock cube's \"reduced sodium\"/\"low-salt\" qualifier made "
              "the matcher abstain to 0mg sodium instead of using the reduced-salt composition data "
              "we do have -- fixed via alias. 1 remaining case needs a code fix (the \"X or Y\" "
              "alternative-ingredient path strips the qualifier before the alias check runs) -- "
              "flagged, not fixed.",
              "- `beef chuck roast`/`chuck roast` were rejected as `top_candidate_incompatible` "
              "despite the correct raw-beef-chuck row being the top embedding candidate (an "
              "over-strict guard, not a data problem) -- fixed via alias (2 recipes).",
              "", "## Not fixed -- found via `outliers.csv`, larger than today's scope", "",
              "The kcal-outlier check surfaced ~50 MyPlate recipes computing near-zero kcal/serving "
              "despite a plausible published reference. Traced to two separate, bigger issues, not "
              "individually fixed:", "",
              "- **Canned/dry legume ambiguity** (~20+ recipes): black beans, kidney beans, lima "
              "beans, white beans, great northern beans, black-eyed peas all abstain with "
              "`ambiguous_preparation_state` -- a deliberate safety design (dry vs. canned/cooked "
              "calorie density differs ~3x, so the pipeline refuses to guess rather than risk a 3x "
              "error) -- but the practical effect is the dish's main ingredient silently contributes "
              "~0 nutrition instead of an estimate. Needs a parser-level fix (detect \"canned\" vs "
              "\"dry\" from the ingredient line) or a documented default assumption, not an alias.",
              "- **The reduced-sodium/low-salt qualifier abstention is broader than stock cubes** -- "
              "also seen on `low-sodium sweet potatoes`, `low-sodium tomatoes`, `low-sodium chicken "
              "broth`, etc. Same root pattern as the stock-cube fix above but not exhaustively "
              "swept; only the stock-cube cases were fixed today.",
              "- **Composition-table gaps**: e.g. `mirin` has no entry at all in the EU composite "
              "table (top embedding candidate was nonsensically \"Kidney, calf, raw\") -- needs new "
              "reference data sourced, not a code or alias fix.",
              "", "See `myplate/outliers.csv` for the full list."]
    (OUT_DIR / "README.md").write_text("\n".join(lines))

    print(f"\nWrote {len(results)} dataset folders + README.md to {OUT_DIR}")


if __name__ == "__main__":
    main()
