#!/usr/bin/env python3
"""Merge RCSI SafeFood lab nutrition onto matching safefood.net web recipes.

The operational SafeFood catalogue is the 334-recipe web scrape. The RCSI lab
workbook contains a smaller set of the same recipes with a complete nutrient
panel. This script stores the lab panel on the matching web recipe IDs as
``nutrition_source='safefood_rcsi'`` so API responses and plots can use the lab
nutrition without switching back to the legacy lab-only recipe nodes.

Dry-run:
    PYTHONPATH=src uv run python scripts/merge_safefood_rcsi_lab.py

Write Postgres + Neo4j metadata:
    PYTHONPATH=src uv run python scripts/merge_safefood_rcsi_lab.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.nutrition_postgres import upsert_recipe_profiling_trace  # noqa: E402
from safefood_rcsi import (  # noqa: E402
    LAB_NUTRITION_SOURCE,
    SOURCE,
    DEFAULT_LAB_XLSX,
    lab_total_nutrients,
    load_rcsi_lab_recipes,
    load_safefood_web_recipes,
    match_lab_to_web,
    rcsi_trace,
)


def _update_neo4j_metadata(rows: list[tuple[str, dict]]) -> None:
    from recipe_wrangler.repositories.neo4j_recipes import driver

    with driver.session() as session:
        for recipe_id, metadata in rows:
            session.run(
                """
                MATCH (r:Recipe)
                WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
                SET r.has_rcsi_lab_nutrition = true,
                    r.ground_truth_nutrition_source = $nutrition_source,
                    r.rcsi_lab_recipe_id = $lab_recipe_id,
                    r.rcsi_lab_title = $lab_title,
                    r.rcsi_lab_match_method = $match_method,
                    r.rcsi_lab_match_score = $match_score,
                    r.cost_category = $cost_category
                """,
                recipe_id=recipe_id,
                nutrition_source=LAB_NUTRITION_SOURCE,
                lab_recipe_id=metadata.get("lab_recipe_id"),
                lab_title=metadata.get("lab_title"),
                match_method=metadata.get("match_method"),
                match_score=metadata.get("match_score"),
                cost_category=metadata.get("cost_category"),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Write Postgres rows and Neo4j metadata.")
    parser.add_argument("--lab-xlsx", type=Path, default=DEFAULT_LAB_XLSX)
    parser.add_argument("--report", type=Path, default=REPO_ROOT / "exports" / "safefood_rcsi_merge_report.json")
    parser.add_argument("--fuzzy-cutoff", type=float, default=0.88)
    args = parser.parse_args()

    lab_recipes = load_rcsi_lab_recipes(args.lab_xlsx)
    web_recipes = load_safefood_web_recipes()
    matches, unmatched = match_lab_to_web(lab_recipes, web_recipes, fuzzy_cutoff=args.fuzzy_cutoff)
    print(
        f"lab={len(lab_recipes)} web={len(web_recipes)} "
        f"matches={len(matches)} unmatched={len(unmatched)}",
        flush=True,
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    neo4j_updates: list[tuple[str, dict]] = []
    report = {
        "nutrition_source": LAB_NUTRITION_SOURCE,
        "lab_count": len(lab_recipes),
        "web_count": len(web_recipes),
        "matched_count": len(matches),
        "unmatched_count": len(unmatched),
        "matches": [],
        "unmatched_lab_titles": [],
    }

    for lab, web, method, score in matches:
        match_meta = {"method": method, "score": round(score, 4)}
        trace = rcsi_trace(lab, web, match_meta)
        report["matches"].append(
            {
                "recipe_id": web.recipe_id,
                "web_title": web.title,
                "web_url": web.url,
                "lab_recipe_id": lab.recipe_id_src,
                "lab_title": lab.title,
                "match_method": method,
                "match_score": round(score, 4),
                "cost_category": lab.cost_category,
            }
        )
        neo4j_updates.append(
            (
                web.recipe_id,
                {
                    "lab_recipe_id": lab.recipe_id_src,
                    "lab_title": lab.title,
                    "match_method": method,
                    "match_score": round(score, 4),
                    "cost_category": lab.cost_category,
                },
            )
        )
        if args.write:
            upsert_recipe_profiling_trace(
                {
                    "recipe_id": web.recipe_id,
                    "title": web.title,
                    "source": SOURCE,
                    "nutrition_source": LAB_NUTRITION_SOURCE,
                    "total_nutrients": lab_total_nutrients(lab),
                    "total_nutrients_per_serving": lab.ground_truth_per_serving,
                    "nutri_score": None,
                    "nutri_score_breakdown": None,
                    "nutrition_profiling_details": None,
                    "nutrition_profiling_debug": {
                        "source": "safefood_rcsi",
                        "source_label": "RCSI SafeFood lab nutrition",
                        "match": match_meta,
                    },
                    "trace": trace,
                    "pipeline_version": "safefood_rcsi_ground_truth",
                    "computed_at": now_iso,
                }
            )

    for lab in unmatched:
        report["unmatched_lab_titles"].append(
            {
                "lab_recipe_id": lab.recipe_id_src,
                "raw_title": lab.raw_title,
                "clean_title": lab.title,
                "normalized_title": lab.normalized_title,
            }
        )

    if args.write:
        _update_neo4j_metadata(neo4j_updates)
        print(f"wrote {len(matches)} {LAB_NUTRITION_SOURCE} Postgres rows + Neo4j metadata", flush=True)
    else:
        print("dry-run only; pass --write to persist", flush=True)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        report_label = args.report.relative_to(REPO_ROOT)
    except ValueError:
        report_label = args.report
    print(f"report: {report_label}", flush=True)
    if unmatched:
        print("unmatched lab titles:", flush=True)
        for item in report["unmatched_lab_titles"]:
            print(f"  - {item['clean_title']}", flush=True)


if __name__ == "__main__":
    main()
