#!/usr/bin/env python3
"""Build Hungarian nutritional ingredients Chroma collection from normalized CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.get_embeddings import get_embeddings_batch


DEFAULT_INPUT = Path("data/processed/hungarian-comp-table/hungarian_comp_table.csv")
DEFAULT_COLLECTION = "nutritional_ingredients_hungarian"
DEFAULT_BATCH_SIZE = 256


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Input CSV not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            canonical_food_id = str(row.get("canonical_food_id") or "").strip()
            food_name = str(row.get("Food Name") or "").strip()
            if not canonical_food_id or not food_name:
                continue
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Chroma collection for Hungarian nutrition ingredient matching."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not delete/recreate collection; append/upsert into existing one.",
    )
    args = parser.parse_args()

    rows = _read_rows(args.input)
    if not rows:
        raise RuntimeError(f"No valid rows found in {args.input}")

    client = get_chroma_client()
    if not args.no_recreate:
        try:
            client.delete_collection(args.collection)
            print(f"deleted_collection={args.collection}")
        except Exception:
            pass
        col = client.create_collection(name=args.collection)
    else:
        col = client.get_or_create_collection(name=args.collection)

    total = len(rows)
    upserted = 0
    for i in range(0, total, args.batch_size):
        batch = rows[i : i + args.batch_size]
        ids = [str(r["canonical_food_id"]).strip() for r in batch]
        docs = [str(r["Food Name"]).strip() for r in batch]
        embs = get_embeddings_batch(docs)
        metas = []
        for row in batch:
            meta = dict(row)
            meta["title"] = str(row.get("Food Name") or "").strip()
            metas.append(meta)

        col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
        upserted += len(batch)
        if upserted % (args.batch_size * 20) == 0:
            print(f"progress={upserted}/{total}")

    print(f"input_rows={len(rows)}")
    print(f"collection={args.collection}")
    print(f"collection_count={col.count()}")


if __name__ == "__main__":
    main()
