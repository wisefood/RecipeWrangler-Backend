#!/usr/bin/env python3
"""Backfill sustainability for profiles where total_sustainability IS NULL.

Corrected for the current schema: ingredient name lives under `name` (the older
script read `ingredient`), and the sustainability weight under
`sustainability_weight_g` (falling back to `weight_g`). Reuses the already-stored
ingredient weights — no re-weighing, no LLM. Idempotent / resumable: it only
touches rows that still have NULL sustainability.
"""

import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine, _get_config
from recipe_wrangler.tools.sustainability_calculator import sustainability_tool_chroma


def main():
    cfg = _get_config()
    schema, table = cfg["schema"], cfg["profiles_table"]
    eng = get_engine()

    with eng.connect() as c:
        keys = c.execute(text(f'''
            SELECT recipe_id, nutrition_source
            FROM "{schema}"."{table}"
            WHERE total_sustainability IS NULL
              AND jsonb_typeof(nutrition_profiling_details) = 'array'
        ''')).all()
    total = len(keys)
    print(f"rows to backfill: {total}", flush=True)

    done = skipped = errors = 0
    t0 = time.time()
    for i, (rid, src) in enumerate(keys, 1):
        try:
            with eng.connect() as c:
                row = c.execute(text(f'''
                    SELECT title, nutrition_profiling_details, total_nutrients,
                           total_nutrients_per_serving, trace
                    FROM "{schema}"."{table}"
                    WHERE recipe_id=:r AND nutrition_source=:s
                '''), {"r": rid, "s": src}).mappings().first()
            if not row:
                skipped += 1
                continue
            det = row["nutrition_profiling_details"] or []
            names, weights = [], []
            for d in det:
                nm = d.get("name") or d.get("ingredient")
                if not nm:
                    continue
                w = d.get("sustainability_weight_g")
                if w is None:
                    w = d.get("weight_g")
                names.append(nm)
                weights.append(float(w or 0))
            if not names:
                skipped += 1
                continue

            tn = row["total_nutrients"] or {}
            ps = row["total_nutrients_per_serving"] or {}
            et = tn.get("energy_kcal") or tn.get("Energy")
            ep = ps.get("energy_kcal") or ps.get("Energy")
            serves = float(et) / float(ep) if et and ep and float(ep) > 0 else 1.0

            res = sustainability_tool_chroma.invoke({
                "title": row["title"], "ingredient_names": names,
                "weights": weights, "serves": serves, "min_similarity": 0.5,
            })

            trace = row["trace"] or {}
            if isinstance(trace, dict) and "profiling" in trace:
                trace["profiling"]["sustainability_profiling_details"] = res["details"]

            with eng.connect() as c:
                c.execute(text(f'''
                    UPDATE "{schema}"."{table}" SET
                        total_sustainability = :a,
                        total_sustainability_per_serving = :b,
                        sustainability_per_kg = :c,
                        sustainability_profiling_details = CAST(:d AS jsonb),
                        trace = CAST(:e AS jsonb),
                        updated_at = now()
                    WHERE recipe_id=:r AND nutrition_source=:s
                '''), {
                    "a": res["total_sustainability"],
                    "b": res["total_sustainability_per_serving"],
                    "c": res["sustainability_per_kg"],
                    "d": json.dumps(res["details"]),
                    "e": json.dumps(trace),
                    "r": rid, "s": src,
                })
                c.commit()
            done += 1
        except Exception as e:
            errors += 1
            print(f"[{i}] ERROR {rid}/{src}: {str(e)[:140]}", flush=True)

        if i % 200 == 0 or i == total:
            rate = i / max(time.time() - t0, 1e-6)
            eta = (total - i) / max(rate, 1e-6)
            print(f"[{i}/{total}] done={done} skipped={skipped} errors={errors} "
                  f"| {rate:.1f}/s ETA {eta/60:.1f}m", flush=True)

    print(f"FINISHED: done={done} skipped={skipped} errors={errors} of {total}", flush=True)


if __name__ == "__main__":
    main()
