#!/usr/bin/env python3
"""Import profiling pipeline data files into the pipeline_static_data Postgres table.

Creates the table if it doesn't exist, then upserts each file by name.
CSV and JSONL files are converted to JSON arrays. JSON files are stored as-is.

Usage:
    python3 scripts/postgres/import_pipeline_static_data.py
    python3 scripts/postgres/import_pipeline_static_data.py --dry-run
    python3 scripts/postgres/import_pipeline_static_data.py --only reviewed_portion_foods,healthyfoods_nutrition
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

import psycopg2  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import _get_config  # noqa: E402

# ---------------------------------------------------------------------------
# Files to import: (name, path, format)
# ---------------------------------------------------------------------------
FILES = [
    (
        "usda-weights-v2",
        REPO_ROOT / "data/nutrition/composition_tables/usda/processed/usda-weights-v2.json",
        "json",
    ),
    (
        "unit_volume_ml_ground_truth",
        REPO_ROOT / "data/nutrition/matching/aliases/unit_volume_ml_ground_truth.json",
        "json",
    ),
    (
        "ingredient_unit_grams_fda",
        REPO_ROOT / "data/nutrition/matching/aliases/ingredient_unit_grams_fda.csv",
        "csv",
    ),
    (
        "ingredient_unit_grams_llm",
        REPO_ROOT / "data/nutrition/matching/aliases/ingredient_unit_grams.csv",
        "csv",
    ),
    (
        "ingredient_unit_reference_dataset",
        REPO_ROOT / "data/portions/weight_reference/ingredient_unit_reference_dataset.csv",
        "csv",
    ),
    (
        "ingredient_composition_aliases",
        REPO_ROOT / "data/nutrition/matching/aliases/ingredient_composition_aliases.csv",
        "csv",
    ),
    (
        "reviewed_portion_foods",
        REPO_ROOT / "data/portions/weight_reference/reviewed_portion_foods.json",
        "json",
    ),
    (
        "structured_external_portions",
        REPO_ROOT / "data/portions/weight_reference/structured_external_portions.jsonl",
        "jsonl",
    ),
    (
        "healthyfoods_nutrition",
        REPO_ROOT / "data/recipes/healthyfoods/HealthyFood_recipes_nutrition.json",
        "json",
    ),
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_static_data (
    name        TEXT PRIMARY KEY,
    content     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

UPSERT_SQL = """
INSERT INTO pipeline_static_data (name, content, updated_at)
VALUES (%s, %s, NOW())
ON CONFLICT (name) DO UPDATE
    SET content    = EXCLUDED.content,
        updated_at = NOW();
"""


def _load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Parse files but do not write to Postgres.")
    parser.add_argument("--only", help="Comma-separated entry names to import; default is all.")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None

    cfg = _get_config()
    if not args.dry_run:
        conn = psycopg2.connect(
            host=cfg["db_host"], port=cfg["db_port"], dbname=cfg["db_name"],
            user=cfg["db_user"], password=cfg["db_password"],
        )
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        print("Table pipeline_static_data ready.")

    for name, path, fmt in FILES:
        if only and name not in only:
            continue
        if not path.exists():
            print(f"  MISSING  {name}  ({path})")
            continue

        size_mb = path.stat().st_size / 1_048_576
        print(f"  Loading  {name}  ({size_mb:.1f} MB) ...", end=" ", flush=True)

        if fmt == "json":
            content = _load_json(path)
        elif fmt == "jsonl":
            content = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            content = _load_csv(path)

        row_count = len(content) if isinstance(content, list) else "object"
        print(f"{row_count} rows", end="")

        if not args.dry_run:
            cur.execute(UPSERT_SQL, (name, json.dumps(content)))
            conn.commit()
            print("  → upserted")
        else:
            print("  → dry-run, skipped")

    if not args.dry_run:
        cur.close()
        conn.close()
        print("\nAll done.")
    else:
        print("\nDry-run complete — no writes.")


if __name__ == "__main__":
    main()
