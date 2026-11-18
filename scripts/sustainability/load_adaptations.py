#!/usr/bin/env python3
"""Load recipe_adaptations rows (mode=sustainability, 4 regions per recipe) built by analysis/sustainability-adaptation-2026-11-17/build_region_rows.py.

Default is a DRY RUN: the file is validated and summarised, nothing touches the database.
`--apply` upserts the rows into recipe_adaptations in ONE transaction (the table must exist: alembic upgrade head). Idempotent: re-running overwrites the same keys.
Usage: load_adaptations.py [--in FILE] [--apply]
"""
import argparse, json, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
DEFAULT = ROOT / 'analysis' / 'sustainability-adaptation-2026-11-17' / 'recipe_adaptations_dryrun.jsonl'


def load(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    keys = Counter((r['recipe_id'], r['mode'], r['region']) for r in rows)
    assert all(v == 1 for v in keys.values()), 'duplicate (recipe_id, mode, region) keys'
    for r in rows:
        assert r['status'] in ('adapted', 'no_suggestion', 'not_scored') and r['region'] in ('IE', 'HU', 'SI', 'EU')
        if r['status'] == 'adapted':
            assert all(r[k] for k in ('original', 'adapted', 'swap', 'benefit')), f"incomplete adapted row {r['recipe_id']} {r['region']}"
    return rows


def apply(rows):
    from sqlalchemy import text
    from recipe_wrangler.utils.nutrition_postgres import get_engine
    sql = text('''INSERT INTO recipe_adaptations (recipe_id, mode, region, version, status, reason_code, original, adapted, swap, benefit, provenance)
        VALUES (:recipe_id, :mode, :region, :version, :status, :reason_code, CAST(:original AS jsonb), CAST(:adapted AS jsonb), CAST(:swap AS jsonb), CAST(:benefit AS jsonb), CAST(:provenance AS jsonb))
        ON CONFLICT (recipe_id, mode, region) DO UPDATE SET version = EXCLUDED.version, status = EXCLUDED.status, reason_code = EXCLUDED.reason_code,
        original = EXCLUDED.original, adapted = EXCLUDED.adapted, swap = EXCLUDED.swap, benefit = EXCLUDED.benefit, provenance = EXCLUDED.provenance, updated_at = now()''')
    with get_engine().begin() as conn:  # one transaction: all rows or none
        for row in rows:
            conn.execute(sql, {k: (json.dumps(v, ensure_ascii=False) if k in ('original', 'adapted', 'swap', 'benefit', 'provenance') and v is not None else v) for k, v in row.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='src', default=str(DEFAULT)); ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    rows = load(args.src)
    print(len(rows), 'rows,', dict(sorted(Counter((r['region'], r['status']) for r in rows).items())))
    if args.apply:
        apply(rows)
        print('upserted into recipe_adaptations')
    else:
        print('dry run: database not touched (use --apply after alembic upgrade head)')


if __name__ == '__main__':
    main()
