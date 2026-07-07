#!/usr/bin/env python3
"""Generate normalized parsing sensitivity comparisons."""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "output" / "sensitivity"
DOC_PATH = REPO_ROOT / "docs" / "sensitivity_analysis_findings.md"

PARSE_FILES = {
    "mistral24b": OUT_DIR / "safefood_parsing_mistral24b.jsonl",
    "qwen14b": OUT_DIR / "safefood_parsing_qwen14b.jsonl",
    "llama31_8b": OUT_DIR / "safefood_parsing_llama31_8b.jsonl",
}
MIN_BASELINE_ROWS = 334


UNIT_MAP = {
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tbsp.": "tbsp",
    "tbsp": "tbsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tsp.": "tsp",
    "tsp": "tsp",
    "grams": "g",
    "gram": "g",
    "g.": "g",
    "kilograms": "kg",
    "kilogram": "kg",
    "millilitres": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "milliliter": "ml",
    "litres": "l",
    "liters": "l",
    "litre": "l",
    "liter": "l",
    "ounces": "oz",
    "ounce": "oz",
    "pounds": "lb",
    "pound": "lb",
    "cloves": "clove",
    "tablespoon of": "tbsp",
    "teaspoon of": "tsp",
}

DROP_NAME_TOKENS = {
    "low", "lowfat", "low-fat", "fat", "fresh", "dried", "ground", "chopped",
    "grated", "sliced", "frozen", "freshly", "reduced", "reduced-fat",
}


def _load_best_parse_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows_by_key: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            key = str(row.get("recipe_key") or "")
            if key:
                rows_by_key.setdefault(key, []).append(row)
    best: dict[str, dict[str, Any]] = {}
    for key, attempts in rows_by_key.items():
        ok = [r for r in attempts if r.get("status") == "ok"]
        if ok:
            best[key] = ok[-1]
    return best


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 2:
        return token[:-1]
    return token


def _norm_name(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", " ", str(text or "").lower())
    toks = [t for t in re.split(r"\s+", s) if t]
    out = []
    for tok in toks:
        if tok in DROP_NAME_TOKENS:
            continue
        out.append(_singularize(tok))
    return " ".join(out).strip()


def _norm_num(text: str) -> str:
    try:
        v = float(text)
    except ValueError:
        return text
    if v.is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _norm_measurement(text: str) -> str:
    s = str(text or "").strip().lower()
    s = s.replace(" of", "")
    s = re.sub(r"\s+", " ", s)
    for src, dst in sorted(UNIT_MAP.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"\b{re.escape(src)}\b", dst, s)
    s = re.sub(r"(?P<num>\d)(?P<unit>[a-z])", r"\g<num> \g<unit>", s)
    parts = s.split()
    normed = [_norm_num(p) for p in parts]
    return " ".join(normed).strip()


def _jaccard(left: list[str], right: list[str]) -> float:
    a = {x for x in left if x}
    b = {x for x in right if x}
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _index_match_rate(left: list[str], right: list[str]) -> float:
    n = max(len(left), len(right), 1)
    m = min(len(left), len(right))
    return sum(1 for i in range(m) if left[i] == right[i]) / n


def _pair_metrics(name_a: str, rows_a: dict[str, dict[str, Any]], name_b: str, rows_b: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    keys = sorted(set(rows_a) & set(rows_b))
    per_case = []
    for key in keys:
        a = rows_a[key]["parsed"]
        b = rows_b[key]["parsed"]
        a_names = list(a.get("ingredient_names") or [])
        b_names = list(b.get("ingredient_names") or [])
        a_meas = list(a.get("measurements") or [])
        b_meas = list(b.get("measurements") or [])
        a_names_norm = [_norm_name(x) for x in a_names]
        b_names_norm = [_norm_name(x) for x in b_names]
        a_meas_norm = [_norm_measurement(x) for x in a_meas]
        b_meas_norm = [_norm_measurement(x) for x in b_meas]
        row = {
            "recipe_key": key,
            "recipe_name": rows_a[key].get("recipe_name") or rows_b[key].get("recipe_name"),
            "model_a": name_a,
            "model_b": name_b,
            "title_exact": str(a.get("title")) == str(b.get("title")),
            "ingredient_count_equal": len(a_names) == len(b_names),
            "ingredient_names_exact_raw": a_names == b_names,
            "ingredient_names_exact_normalized": a_names_norm == b_names_norm,
            "measurements_exact_raw": a_meas == b_meas,
            "measurements_exact_normalized": a_meas_norm == b_meas_norm,
            "ingredient_name_index_match_rate_raw": _index_match_rate(a_names, b_names),
            "ingredient_name_index_match_rate_normalized": _index_match_rate(a_names_norm, b_names_norm),
            "measurement_index_match_rate_raw": _index_match_rate(a_meas, b_meas),
            "measurement_index_match_rate_normalized": _index_match_rate(a_meas_norm, b_meas_norm),
            "ingredient_name_jaccard_raw": _jaccard(a_names, b_names),
            "ingredient_name_jaccard_normalized": _jaccard(a_names_norm, b_names_norm),
            "measurement_jaccard_raw": _jaccard(a_meas, b_meas),
            "measurement_jaccard_normalized": _jaccard(a_meas_norm, b_meas_norm),
            "serves_equal": a.get("serves") == b.get("serves"),
            "time_equal": a.get("total_time") == b.get("total_time"),
            "names_a_raw": a_names,
            "names_b_raw": b_names,
            "names_a_norm": a_names_norm,
            "names_b_norm": b_names_norm,
            "meas_a_raw": a_meas,
            "meas_b_raw": b_meas,
            "meas_a_norm": a_meas_norm,
            "meas_b_norm": b_meas_norm,
        }
        per_case.append(row)

    def _mean(key: str) -> float:
        return statistics.fmean(float(r[key]) for r in per_case) if per_case else 0.0

    summary = {
        "comparison": f"{name_a}_vs_{name_b}",
        "recipes_compared": len(per_case),
        "title_exact_rate": sum(1 for r in per_case if r["title_exact"]) / max(len(per_case), 1),
        "ingredient_names_exact_raw_rate": sum(1 for r in per_case if r["ingredient_names_exact_raw"]) / max(len(per_case), 1),
        "ingredient_names_exact_normalized_rate": sum(1 for r in per_case if r["ingredient_names_exact_normalized"]) / max(len(per_case), 1),
        "measurements_exact_raw_rate": sum(1 for r in per_case if r["measurements_exact_raw"]) / max(len(per_case), 1),
        "measurements_exact_normalized_rate": sum(1 for r in per_case if r["measurements_exact_normalized"]) / max(len(per_case), 1),
        "ingredient_count_equal_rate": sum(1 for r in per_case if r["ingredient_count_equal"]) / max(len(per_case), 1),
        "mean_ingredient_name_index_match_rate_raw": _mean("ingredient_name_index_match_rate_raw"),
        "mean_ingredient_name_index_match_rate_normalized": _mean("ingredient_name_index_match_rate_normalized"),
        "mean_measurement_index_match_rate_raw": _mean("measurement_index_match_rate_raw"),
        "mean_measurement_index_match_rate_normalized": _mean("measurement_index_match_rate_normalized"),
        "mean_ingredient_name_jaccard_raw": _mean("ingredient_name_jaccard_raw"),
        "mean_ingredient_name_jaccard_normalized": _mean("ingredient_name_jaccard_normalized"),
        "mean_measurement_jaccard_raw": _mean("measurement_jaccard_raw"),
        "mean_measurement_jaccard_normalized": _mean("measurement_jaccard_normalized"),
        "serves_equal_rate": sum(1 for r in per_case if r["serves_equal"]) / max(len(per_case), 1),
        "time_equal_rate": sum(1 for r in per_case if r["time_equal"]) / max(len(per_case), 1),
    }
    return summary, per_case


def main() -> None:
    available = {name: path for name, path in PARSE_FILES.items() if path.exists()}
    loaded = {name: _load_best_parse_rows(path) for name, path in available.items()}

    outputs = []
    pairs = [("mistral24b", "qwen14b")]
    if "llama31_8b" in loaded and len(loaded["llama31_8b"]) >= MIN_BASELINE_ROWS:
        pairs.extend([("llama31_8b", "mistral24b"), ("llama31_8b", "qwen14b")])

    all_summaries = {}
    for a, b in pairs:
        if a not in loaded or b not in loaded:
            continue
        summary, per_case = _pair_metrics(a, loaded[a], b, loaded[b])
        stem = f"parse_sensitivity_normalized_{a}_vs_{b}"
        jsonl_path = OUT_DIR / f"{stem}.jsonl"
        json_path = OUT_DIR / f"{stem}.json"
        with jsonl_path.open("w", encoding="utf-8") as handle:
            for row in per_case:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        all_summaries[summary["comparison"]] = summary
        outputs.extend([jsonl_path, json_path])

    md = [
        "## Parsing Normalized Re-Analysis",
        "",
        "These parsing comparisons use normalized ingredient and measurement forms to reduce sensitivity to superficial formatting differences.",
        "",
    ]
    for name, summary in all_summaries.items():
        md += [
            f"### {name}",
            "",
            f"- recipes compared: `{summary['recipes_compared']}`",
            f"- title exact rate: `{summary['title_exact_rate']*100:.1f}%`",
            f"- ingredient exact raw: `{summary['ingredient_names_exact_raw_rate']*100:.1f}%`",
            f"- ingredient exact normalized: `{summary['ingredient_names_exact_normalized_rate']*100:.1f}%`",
            f"- measurement exact raw: `{summary['measurements_exact_raw_rate']*100:.1f}%`",
            f"- measurement exact normalized: `{summary['measurements_exact_normalized_rate']*100:.1f}%`",
            f"- ingredient index match raw: `{summary['mean_ingredient_name_index_match_rate_raw']*100:.1f}%`",
            f"- ingredient index match normalized: `{summary['mean_ingredient_name_index_match_rate_normalized']*100:.1f}%`",
            f"- measurement index match raw: `{summary['mean_measurement_index_match_rate_raw']*100:.1f}%`",
            f"- measurement index match normalized: `{summary['mean_measurement_index_match_rate_normalized']*100:.1f}%`",
            "",
        ]
    DOC_PATH.write_text(DOC_PATH.read_text(encoding="utf-8") + "\n" + "\n".join(md) + "\n", encoding="utf-8")
    for path in outputs:
        print(f"Wrote {path}")
    print(f"Updated {DOC_PATH}")


if __name__ == "__main__":
    main()
