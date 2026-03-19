#!/usr/bin/env python3
"""Rebuild usda_ingredients_canonical using usda_food_label embeddings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.get_embeddings import get_embeddings_batch


def _clean_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild a Chroma collection from recipe1m-usda-links-canonical using usda_food_label text."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/mappings/recipe1m-usda-links-canonical.json"),
        help="Input canonical-usda links JSON.",
    )
    parser.add_argument(
        "--collection",
        default="usda_ingredients_canonical",
        help="Target Chroma collection name.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Embedding/upsert batch size.",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not delete/recreate collection; append/upsert into existing one.",
    )
    args = parser.parse_args()

    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"Expected JSON array in {args.input}")

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

    cleaned = []
    for row in rows:
        canonical_id = str(row.get("canonical_id") or "").strip()
        canonical = _clean_text(str(row.get("canonical") or ""))
        usda_id = str(row.get("usda_id") or "").strip()
        usda_food_label = _clean_text(str(row.get("usda_food_label") or ""))
        if not canonical_id or not usda_food_label:
            continue
        cleaned.append(
            {
                "id": f"canon-{canonical_id}",
                "document": usda_food_label,
                "metadata": {
                    "canonical_id": canonical_id,
                    "canonical": canonical,
                    "name": canonical,
                    "usda_id": usda_id,
                    "usda_food_label": usda_food_label,
                    "food_group_id": str(row.get("food_group_id") or ""),
                    "food_group": str(row.get("food_group") or ""),
                    "type": "usda_food_label",
                },
            }
        )

    total = len(cleaned)
    added = 0
    for i in range(0, total, args.batch_size):
        batch = cleaned[i:i + args.batch_size]
        docs: List[str] = [x["document"] for x in batch]
        embs = get_embeddings_batch(docs)
        col.upsert(
            ids=[x["id"] for x in batch],
            documents=docs,
            embeddings=embs,
            metadatas=[x["metadata"] for x in batch],
        )
        added += len(batch)
        if added % (args.batch_size * 20) == 0:
            print(f"progress={added}/{total}")

    print(f"input_rows={len(rows)}")
    print(f"valid_rows={total}")
    print(f"collection={args.collection}")
    print(f"collection_count={col.count()}")


if __name__ == "__main__":
    main()

