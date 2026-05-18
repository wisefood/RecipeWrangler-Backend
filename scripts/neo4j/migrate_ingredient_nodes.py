#!/usr/bin/env python3
"""Migrate Neo4j Ingredient nodes to a clean, deduplicated shape.

Target schema after migration::

    (Recipe)-[:HAS_INGREDIENT {measurement, unit, quantity, weight_grams}]->(Ingredient {name, canonical_id})

The canonical clean name comes from Postgres
``nutrition_profiling_details[].matched_sustainability_ingredient`` for the
57,919 recipes profiled under ``pipeline_version='recompute_2026-05-11'``.
Unprofiled Recipe1M recipes (~753k) are out of scope of this script — handle
separately. FoodHero is also skipped in the write phase because its raw input
already concatenated multiple ingredients into single strings (re-import
required).

Phases (each is independent and read-only unless ``--write`` is passed)::

    --phase validate   sample 200 recipes per source, report join rate
    --phase plan       full plan JSONL per source
    --phase write      execute migration (requires --write to actually mutate)
    --phase verify     post-migration top-N + counts

Default is dry-run. Resumable via per-source checkpoint files under
``data_to_send/migration/``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_engine  # noqa: E402

load_runtime_env()

from neo4j import GraphDatabase  # noqa: E402

DEFAULT_PIPELINE_VERSION = "recompute_2026-05-11"
SOURCES_PROFILED = ("HealthyFoods", "MyPlate", "Irish_SafeFood", "recipe1m")
SOURCES_SKIP_WRITE = ("FoodHero",)  # see module docstring
OUT_DIR = REPO_ROOT / "data_to_send" / "migration"
SAMPLE_SIZE = 200
DEFAULT_BATCH = 500


def _norm_name(s: object) -> str | None:
    if not s:
        return None
    out = str(s).strip().lower()
    return out or None


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _split_measurement(meas: str | None) -> tuple[float | None, str | None]:
    """Best-effort split of a measurement string into (quantity, unit)."""
    if not meas:
        return None, None
    parts = str(meas).strip().split(None, 1)
    if not parts:
        return None, None
    try:
        q = float(parts[0].replace(",", "."))
        unit = parts[1] if len(parts) > 1 else None
        return q, unit
    except ValueError:
        return None, str(meas)


def _driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )


# --------------------------------------------------------------------------- #
# Postgres → plan rows
# --------------------------------------------------------------------------- #
def _iter_plan_rows(
    source: str, pipeline_version: str, limit: int | None
) -> Iterator[dict]:
    eng = get_engine()
    cfg = _get_config()
    q = f"""
        SELECT recipe_id, nutrition_profiling_details
        FROM "{cfg['schema']}"."{cfg['profiles_table']}"
        WHERE pipeline_version = :pv
          AND source = :s
          AND nutrition_source = 'usda'
          AND nutrition_profiling_details IS NOT NULL
        ORDER BY recipe_id
        {f'LIMIT {int(limit)}' if limit else ''}
    """
    with eng.connect() as c:
        for recipe_id, details in c.execute(text(q), {"pv": pipeline_version, "s": source}):
            if not isinstance(details, list):
                continue
            seen_raw_in_recipe: dict[str, int] = defaultdict(int)
            for entry in details:
                if not isinstance(entry, dict):
                    continue
                raw = _norm_name(entry.get("name"))
                clean = _norm_name(entry.get("matched_sustainability_ingredient"))
                if not raw:
                    continue
                if not clean:
                    # fall back to matched_nutritional_ingredient as a secondary signal
                    clean = _norm_name(entry.get("matched_nutritional_ingredient"))
                if not clean:
                    continue
                seen_raw_in_recipe[raw] += 1
                meas = entry.get("measurement")
                qty, unit = _split_measurement(meas)
                yield {
                    "recipe_id": recipe_id,
                    "raw_name": raw,
                    "raw_occurrence": seen_raw_in_recipe[raw],
                    "clean_name": clean,
                    "measurement": meas,
                    "quantity": qty,
                    "unit": unit,
                    "weight_grams": _f(entry.get("weight_g")),
                }


# --------------------------------------------------------------------------- #
# Phase: validate
# --------------------------------------------------------------------------- #
def phase_validate(args) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"pipeline_version": args.pipeline_version, "sources": {}}
    drv = _driver()
    try:
        with drv.session() as s:
            for source in args.sources:
                eng = get_engine()
                cfg = _get_config()
                with eng.connect() as c:
                    rec_ids = [
                        r[0]
                        for r in c.execute(
                            text(
                                f'SELECT DISTINCT recipe_id FROM "{cfg["schema"]}"."{cfg["profiles_table"]}" '
                                f"WHERE pipeline_version = :pv AND source = :s"
                            ),
                            {"pv": args.pipeline_version, "s": source},
                        )
                    ]
                if not rec_ids:
                    report["sources"][source] = {"recipes_in_postgres": 0}
                    continue
                sample = random.sample(rec_ids, min(SAMPLE_SIZE, len(rec_ids)))
                # build Postgres expected ingredient names per recipe
                pg_by_recipe: dict[str, set[str]] = defaultdict(set)
                for row in _iter_plan_rows(source, args.pipeline_version, None):
                    if row["recipe_id"] in sample:
                        pg_by_recipe[row["recipe_id"]].add(row["raw_name"])
                # fetch Neo4j ingredient names for the same recipes
                neo_by_recipe: dict[str, set[str]] = defaultdict(set)
                concat_count = 0
                for rec_id in sample:
                    result = s.run(
                        "MATCH (r:Recipe {recipe_id: $rid})-[:HAS_INGREDIENT]->(i:Ingredient) "
                        "RETURN i.name AS n",
                        rid=rec_id,
                    )
                    for r in result:
                        nm = _norm_name(r["n"])
                        if nm:
                            neo_by_recipe[rec_id].add(nm)
                            if len(nm) > 60 or nm.count(",") >= 3:
                                concat_count += 1
                # join rate
                joined = total = 0
                for rec_id, names in pg_by_recipe.items():
                    neo = neo_by_recipe.get(rec_id, set())
                    joined += len(names & neo)
                    total += len(names)
                report["sources"][source] = {
                    "recipes_in_postgres": len(rec_ids),
                    "sampled": len(sample),
                    "pg_ingredient_total": total,
                    "joined": joined,
                    "join_rate": round(joined / total, 4) if total else 0.0,
                    "neo4j_concat_lines": concat_count,
                }
    finally:
        drv.close()

    out = OUT_DIR / "validate_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Phase: plan
# --------------------------------------------------------------------------- #
def phase_plan(args) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for source in args.sources:
        out = OUT_DIR / f"plan_{source}.jsonl"
        n = 0
        with out.open("w") as f:
            for row in _iter_plan_rows(source, args.pipeline_version, args.limit):
                f.write(json.dumps(row, default=str) + "\n")
                n += 1
        print(f"{source}: wrote {n} plan rows → {out.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# Phase: write
# --------------------------------------------------------------------------- #
REWRITE_CYPHER = """
UNWIND $rows AS row
CALL (row) {
    MATCH (r:Recipe {recipe_id: row.recipe_id})-[h:HAS_INGREDIENT]->(old:Ingredient {name: row.raw_name})
    WITH r, h, row LIMIT 1
    MERGE (clean:Ingredient {name: row.clean_name})
        ON CREATE SET clean.canonical_id = randomUUID()
    CREATE (r)-[h2:HAS_INGREDIENT]->(clean)
    SET h2.measurement  = row.measurement,
        h2.unit         = row.unit,
        h2.quantity     = row.quantity,
        h2.weight_grams = row.weight_grams
    DELETE h
} IN TRANSACTIONS OF 500 ROWS
"""

DROP_ORPHANS_CYPHER = """
MATCH (i:Ingredient)
WHERE NOT (:Recipe)-[:HAS_INGREDIENT]->(i)
  AND NOT (i)<-[:HAS_INGREDIENT_ORIGINAL]-()
  AND NOT (i)-[:HAS_ALLERGEN]-()
  AND NOT (i)-[:HAS_CLASS]-()
  AND NOT (i)-[:HAS_SUBSTITUTION]-()
  AND NOT (i)-[:FLAVORDB_EQUIVALENT]-()
WITH i LIMIT $batch
DETACH DELETE i
RETURN count(*) AS deleted
"""


def phase_write(args) -> None:
    if any(src in SOURCES_SKIP_WRITE for src in args.sources):
        print(f"[migrate] refusing to write for {SOURCES_SKIP_WRITE} — re-import path needed first.")
        return
    if not args.write:
        print("[migrate] --write not set: reporting what would happen, no Cypher will run.")
    drv = _driver()
    try:
        for source in args.sources:
            plan = OUT_DIR / f"plan_{source}.jsonl"
            if not plan.exists():
                print(f"{source}: plan file missing ({plan.relative_to(REPO_ROOT)}). Run --phase plan first.")
                continue
            ckpt = OUT_DIR / f"checkpoint_{source}.jsonl"
            done_keys: set[str] = set()
            if ckpt.exists() and not args.no_resume:
                for line in ckpt.read_text().splitlines():
                    try:
                        rec = json.loads(line)
                        done_keys.add(f"{rec['recipe_id']}|{rec['raw_name']}|{rec['raw_occurrence']}")
                    except json.JSONDecodeError:
                        pass
            print(f"{source}: resume_skip={len(done_keys)}")

            batch: list[dict] = []
            t0 = time.time()
            written = 0
            with plan.open() as f, ckpt.open("a") as ck:
                for line in f:
                    row = json.loads(line)
                    key = f"{row['recipe_id']}|{row['raw_name']}|{row['raw_occurrence']}"
                    if key in done_keys:
                        continue
                    batch.append(row)
                    if len(batch) >= args.batch_size:
                        if args.write:
                            with drv.session() as s:
                                s.run(REWRITE_CYPHER, rows=batch).consume()
                        written += len(batch)
                        for b in batch:
                            ck.write(json.dumps(b, default=str) + "\n")
                        ck.flush()
                        if written % (args.batch_size * 10) == 0:
                            rate = written / max(1e-6, time.time() - t0)
                            print(f"  {written} rows @ {rate:.0f}/s")
                        batch = []
                if batch:
                    if args.write:
                        with drv.session() as s:
                            s.run(REWRITE_CYPHER, rows=batch).consume()
                    written += len(batch)
                    for b in batch:
                        ck.write(json.dumps(b, default=str) + "\n")
            print(f"{source}: wrote_or_would_write={written}  in {(time.time()-t0)/60:.1f} min")

            if args.write:
                # drop orphans in capped batches
                total_deleted = 0
                while True:
                    with drv.session() as s:
                        rec = s.run(DROP_ORPHANS_CYPHER, batch=5000).single()
                        deleted = rec["deleted"] if rec else 0
                    total_deleted += deleted
                    if not deleted:
                        break
                print(f"{source}: orphan Ingredient nodes deleted={total_deleted}")
    finally:
        drv.close()


# --------------------------------------------------------------------------- #
# Phase: verify
# --------------------------------------------------------------------------- #
def phase_verify(args) -> None:
    drv = _driver()
    report: dict = {}
    try:
        with drv.session() as s:
            report["ingredient_nodes"] = s.run("MATCH (i:Ingredient) RETURN count(i) AS n").single()["n"]
            report["has_ingredient_rels"] = s.run("MATCH ()-[h:HAS_INGREDIENT]->() RETURN count(h) AS n").single()["n"]
            report["per_source"] = {}
            for source in args.sources:
                res = s.run(
                    "MATCH (r:Recipe {source: $s})-[:HAS_INGREDIENT]->(i:Ingredient) "
                    "WITH i.name AS name, count(DISTINCT r) AS n "
                    "RETURN name, n ORDER BY n DESC LIMIT 10",
                    s=source,
                ).data()
                report["per_source"][source] = res
    finally:
        drv.close()
    print(json.dumps(report, indent=2, default=str))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", required=True, choices=("validate", "plan", "write", "verify"))
    p.add_argument("--sources", nargs="+", default=list(SOURCES_PROFILED))
    p.add_argument("--pipeline-version", default=DEFAULT_PIPELINE_VERSION)
    p.add_argument("--limit", type=int, default=None, help="Postgres rows to process per source (plan/write)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    p.add_argument("--write", action="store_true", help="Authorise Cypher writes (default: dry-run)")
    p.add_argument("--no-resume", action="store_true")
    args = p.parse_args()

    handlers = {
        "validate": phase_validate,
        "plan": phase_plan,
        "write": phase_write,
        "verify": phase_verify,
    }
    handlers[args.phase](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
