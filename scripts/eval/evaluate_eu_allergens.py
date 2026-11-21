"""Frozen external silver-label evaluation; CPU keyword detector, no model or DB calls."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "analysis/eu-allergen-2026-11-05"
OUTPUT = ROOT / "evaluation/allergens_diets/results/eu-allergen-2026-11-05"
SEED = "eu-allergen-20261105-v1"
LABEL_MAP = {"gluten": "gluten", "crustacean_shellfish": "crustaceans", "egg": "eggs",
             "fish": "fish", "peanut": "peanuts", "soy": "soy", "milk": "milk",
             "tree_nut": "tree_nuts", "celery": "celery", "mustard": "mustard",
             "sesame": "sesame", "sulphites": "sulphites", "lupin": "lupin", "molluscs": "molluscs"}
LABELS = sorted(LABEL_MAP.values())
CODE = ["src/recipe_wrangler/repositories/neo4j_recipes.py",
        "src/recipe_wrangler/utils/consumer_suitability.py", "src/recipe_wrangler/utils/ingredient_context.py",
        "src/recipe_wrangler/utils/food_ontology.py", "scripts/eval/evaluate_eu_allergens.py"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def strings(value: str) -> list[str]:
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("Expected a list of strings")
    return parsed


def identity(lines: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(re.sub(r"\s+", " ", line.casefold()).strip()
                                              for line in lines), ensure_ascii=False).encode()).hexdigest()


def prepare() -> None:
    download = json.loads((WORK / "download.json").read_text())
    assert digest(WORK / "allergens.csv") == download["sha256"]
    internal_hashes, internal_titles = set(), set()
    for name in ["representative", "hard_cases"]:
        for row in json.loads((ROOT / f"evaluation/allergens_diets/data/frozen-sets/{name}.json").read_text()):
            if row.get("recipe_id"):
                internal_hashes.add(identity(row["original_lines"]))
                internal_titles.add(row["title"].strip().casefold())
    counts, issues, rows, ids, ingredient_hashes = Counter(), [], [], set(), set()
    primary_counts, broad_counts = Counter(), Counter()
    with (WORK / "allergens.csv").open(encoding="utf-8-sig", newline="") as handle:
        for number, raw in enumerate(csv.DictReader(handle), start=2):
            counts["source_rows"] += 1
            flags = []
            try:
                row = {**raw, "csv_line": number,
                       "raw": strings(raw["ingredients"]), "clean": strings(raw["ingredients_clean"]),
                       "primary": sorted(set(strings(raw["allergens_primary"]))),
                       "broad": sorted(set(strings(raw["allergens_all_possible"]))),
                       "optional": strings(raw["optional_ingredients"])}
                alternatives = ast.literal_eval(raw["alternative_groups"])
                if not isinstance(alternatives, list) or not all(
                    isinstance(group, list) and all(isinstance(v, str) for v in group) for group in alternatives
                ):
                    raise ValueError("Malformed alternative groups")
                row["alternatives"] = alternatives
                if not set(row["primary"] + row["broad"]) <= set(LABELS):
                    raise ValueError("Unexpected reference allergen label")
                if not row["raw"] or not row["clean"] or not raw["recipe_id"]:
                    raise ValueError("Missing recipe ID or ingredient input")
            except (ValueError, SyntaxError, TypeError) as error:
                counts["malformed_rows"] += 1
                issues.append({"csv_line": number, "recipe_id": raw.get("recipe_id"), "issues": [str(error)]})
                continue
            counts["parseable_rows"] += 1
            primary_counts.update(row["primary"]); broad_counts.update(row["broad"])
            if not set(row["primary"]) <= set(row["broad"]):
                flags.append("primary_not_subset_of_all_possible")
            if row["optional"]: counts["rows_with_optional"] += 1
            if alternatives: counts["rows_with_alternatives"] += 1
            fingerprint = identity(row["raw"])
            exclusions = []
            if raw["recipe_id"] in ids: exclusions.append("duplicate_recipe_id")
            if fingerprint in ingredient_hashes: exclusions.append("duplicate_raw_ingredient_list")
            if fingerprint in internal_hashes: exclusions.append("exact_internal_ingredient_overlap")
            if raw["title"].strip().casefold() in internal_titles:
                exclusions.append("internal_title_overlap_conservative_exclusion")
            ids.add(raw["recipe_id"]); ingredient_hashes.add(fingerprint)
            for flag in flags + exclusions: counts[flag] += 1
            if flags or exclusions:
                issues.append({"csv_line": number, "recipe_id": raw["recipe_id"], "issues": flags + exclusions})
            if exclusions: continue
            row["quality_flags"] = flags
            row["ingredient_hash"] = fingerprint
            row["rank"] = hashlib.sha256(f"{SEED}:{raw['recipe_id']}:{fingerprint}".encode()).hexdigest()
            rows.append(row)
    rows.sort(key=lambda row: row["rank"])
    # Keep identical titles out of both development and test selections.
    selected, selected_titles = [], set()
    for split, amount in [("pilot", 100), ("random_test", 1000)]:
        chosen = 0
        for row in rows:
            title = row["title"].strip().casefold()
            if title in selected_titles: continue
            selected_titles.add(title)
            selected.append({**row, "split": split, "selection_reason": "deterministic_random"})
            chosen += 1
            if chosen == amount: break
        assert chosen == amount
    for reason in [*LABELS, "optional", "alternatives", "inconsistent_labels"]:
        amount = 10 if reason in LABELS else 20
        chosen = 0
        for row in rows:
            title = row["title"].strip().casefold()
            matches = (reason in row["broad"] or reason in row["primary"]) if reason in LABELS else bool(
                row[{"optional": "optional", "alternatives": "alternatives", "inconsistent_labels": "quality_flags"}[reason]])
            if not matches or title in selected_titles: continue
            selected_titles.add(title)
            selected.append({**row, "split": "challenge", "selection_reason": reason})
            chosen += 1
            if chosen == amount: break
    audit = {"download": download, "counts": dict(counts), "primary_label_counts": dict(primary_counts),
             "all_possible_label_counts": dict(broad_counts), "eligible_unique_rows": len(rows),
             "selection_counts": dict(Counter(r["split"] for r in selected)),
             "reference_issues": issues, "label_mapping": LABEL_MAP, "unscored_internal_label": "wheat"}
    write_json(OUTPUT / "source-audit.json", audit)
    write_json(WORK / "selection.json", selected)
    manifest = {"seed": SEED, "data_sha256": download["sha256"], "revision": download["revision"],
                "selection_sha256": digest(WORK / "selection.json"),
                "code_sha256": {name: digest(ROOT / name) for name in CODE},
                "selection": [{k: row[k] for k in ["recipe_id", "csv_line", "title", "split", "selection_reason", "ingredient_hash", "quality_flags"]} for row in selected]}
    write_json(OUTPUT / "selection-manifest.json", manifest)
    print(json.dumps({**dict(counts), "selected": audit["selection_counts"]}), flush=True)


def check_frozen() -> list[dict]:
    manifest = json.loads((OUTPUT / "selection-manifest.json").read_text())
    assert digest(WORK / "selection.json") == manifest["selection_sha256"]
    for name, expected in manifest["code_sha256"].items():
        assert digest(ROOT / name) == expected, f"Frozen evaluator/detector changed: {name}"
    return json.loads((WORK / "selection.json").read_text())


def infer_row(row: dict) -> dict:
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names
    result = {key: row[key] for key in ["recipe_id", "csv_line", "title", "source", "link", "split", "selection_reason", "quality_flags", "primary", "broad"]}
    for view in ["raw", "clean"]:
        evidence = detect_allergen_evidence_from_names(row[view])
        confirmed = {LABEL_MAP[e["allergen"]] for e in evidence if e["allergen"] in LABEL_MAP and e["presence"] == "contains"}
        possible = {LABEL_MAP[e["allergen"]] for e in evidence if e["allergen"] in LABEL_MAP and e["presence"] != "contains"} - confirmed
        result[view] = {"confirmed": sorted(confirmed), "possible": sorted(possible),
                        "wheat_evidence": [e for e in evidence if e["allergen"] == "wheat"], "evidence": evidence}
    return result


def run_batch(number: int, batch_size: int, workers: int = 1) -> None:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    from recipe_wrangler.utils.food_ontology import CLASSIFICATION_VERSION
    selection = check_frozen()
    started = time.monotonic()
    rows = selection[number * batch_size:(number + 1) * batch_size]
    if workers == 1:
        predictions = [infer_row(row) for row in rows]
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            predictions = list(pool.map(infer_row, rows))
    assert predictions
    write_json(WORK / f"batch-{number:04}.json", {"detector_version": CLASSIFICATION_VERSION,
               "selection_sha256": digest(WORK / "selection.json"), "rows": predictions, "workers": workers,
               "seconds": round(time.monotonic() - started, 3)})
    print(json.dumps({"batch": number, "recipes": len(predictions), "seconds": round(time.monotonic()-started, 3)}), flush=True)


def score(cases: list[dict], view: str, target: str, include_possible: bool) -> dict:
    counts = {label: Counter() for label in LABELS}
    exact = 0
    for row in cases:
        truth = set(row[target])
        predicted = set(row[view]["confirmed"])
        if include_possible: predicted.update(row[view]["possible"])
        exact += truth == predicted
        for label in LABELS:
            counts[label]["tp" if label in truth and label in predicted else
                          "fn" if label in truth else "fp" if label in predicted else "tn"] += 1
    def rates(c: Counter) -> dict:
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        return {**{key: c[key] for key in ["tp", "fp", "fn", "tn"]}, "support": tp + fn,
                "precision": tp/(tp+fp) if tp+fp else None,
                "recall": tp/(tp+fn) if tp+fn else None,
                "f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}
    per_label = {label: rates(c) for label, c in counts.items()}
    total = sum(counts.values(), Counter())
    supported = [r["f1"] for r in per_label.values() if r["support"] > 0]
    return {"recipes": len(cases), "exact_set_matches": exact,
            "exact_set_agreement": exact/len(cases) if cases else None,
            "micro": rates(total), "macro_f1_supported_labels": sum(supported)/len(supported) if supported else None,
            "per_label": per_label}


def merge(batch_size: int) -> None:
    selection = check_frozen()
    cases = []
    for number in range((len(selection) + batch_size - 1)//batch_size):
        batch = json.loads((WORK / f"batch-{number:04}.json").read_text())
        assert batch["selection_sha256"] == digest(WORK / "selection.json")
        cases.extend(batch["rows"])
    assert [r["recipe_id"] for r in cases] == [r["recipe_id"] for r in selection]
    summary = {}
    for split in ["pilot", "random_test", "challenge"]:
        subset = [r for r in cases if r["split"] == split]
        for quality, rows in [("all", subset), ("consistent_reference", [r for r in subset if not r["quality_flags"]])]:
            summary[f"{split}/{quality}"] = {
                name: score(rows, view, target, possible)
                for name, view, target, possible in [
                    ("clean_confirmed_vs_primary", "clean", "primary", False),
                    ("raw_confirmed_vs_primary", "raw", "primary", False),
                    ("raw_flagged_vs_all_possible", "raw", "broad", True)]}
    write_json(OUTPUT / "summary.json", summary)
    with (OUTPUT / "predictions.jsonl").open("w") as handle:
        for row in cases: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (OUTPUT / "expected_vs_pipeline.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["split", "recipe_id", "title", "source", "link", "quality_flags", "expected_primary", "expected_all_possible",
                  "raw_confirmed", "raw_possible", "clean_confirmed", "clean_possible", "primary_clean_missed", "primary_clean_extra",
                  "all_possible_raw_missed", "all_possible_raw_extra"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for row in cases:
            reference, broad = set(row["primary"]), set(row["broad"])
            clean, raw = set(row["clean"]["confirmed"]), set(row["raw"]["confirmed"] + row["raw"]["possible"])
            cells = {k: row[k] for k in ["split", "recipe_id", "title", "source", "link"]}
            cells.update({"quality_flags": row["quality_flags"], "expected_primary": reference, "expected_all_possible": broad,
                          "primary_clean_missed": reference-clean, "primary_clean_extra": clean-reference,
                          "all_possible_raw_missed": broad-raw, "all_possible_raw_extra": raw-broad})
            for view in ["raw", "clean"]:
                for state in ["confirmed", "possible"]: cells[f"{view}_{state}"] = row[view][state]
            writer.writerow({key: "; ".join(sorted(value)) if isinstance(value, (list, set)) else value for key, value in cells.items()})
    manifest = json.loads((OUTPUT / "selection-manifest.json").read_text())
    manifest["output_sha256"] = {name: digest(OUTPUT / name) for name in ["summary.json", "predictions.jsonl", "expected_vs_pipeline.csv", "source-audit.json"]}
    manifest["batch_sha256"] = {f"batch-{n:04}.json": digest(WORK / f"batch-{n:04}.json") for n in range((len(selection)+batch_size-1)//batch_size)}
    write_json(OUTPUT / "run-manifest.json", manifest)
    print(json.dumps({"evaluated": len(cases), "random_test": summary["random_test/all"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "batch", "merge"])
    parser.add_argument("--number", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    if args.batch_size < 1 or args.number < 0: parser.error("Batch size must be positive and number nonnegative")
    if args.action == "prepare": prepare()
    elif args.action == "batch": run_batch(args.number, args.batch_size, args.workers)
    else: merge(args.batch_size)
