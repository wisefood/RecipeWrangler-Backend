#!/usr/bin/env python3
"""Build EU nutritional ingredients Chroma collection from the Postgres table.

Mirrors the Hungarian / Irish / USDA builders. Reads from
`nutrients-ingredients-eu` (loaded by scripts/build_eu_global_dataset.py),
embeds food names with the project-wide embedder (BAAI/bge-small-en-v1.5 per
.env -> EMBED_MODEL_NAME), and upserts into Chroma collection
`nutritional_ingredients_eu` (384-dim, cosine).

ID convention: <source>:<native_code>  e.g. ciqual:19024, cofid:13-145, nevo:1
Metadata stored: {eu_id, food_name, source, country, food_group}.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# Load .env BEFORE importing get_embeddings — the embedder reads EMBED_MODEL_NAME
# at import time, and falls back to Qwen3-Embedding-8B (4096-dim) if unset. We
# need it to pick up bge-small-en-v1.5 (384-dim) so the collection is compatible
# with the existing USDA / Irish / Hungarian collections.
from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402
load_runtime_env()

from recipe_wrangler.utils.chroma_client import get_chroma_client  # noqa: E402
from recipe_wrangler.utils.get_embeddings import get_embeddings_batch  # noqa: E402
from recipe_wrangler.utils.nutrition_postgres import get_engine, _env  # noqa: E402


DEFAULT_TABLE = _env("NUTRITION_EU_TABLE", "nutrients-ingredients-eu")
DEFAULT_COLLECTION = "nutritional_ingredients_eu"
DEFAULT_BATCH_SIZE = 256


def _load_rows(table: str) -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        rs = conn.execute(text(
            f'SELECT id, food_name, source, country, food_group FROM "{table}" ORDER BY id'
        ))
        return [dict(r._mapping) for r in rs]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--no-recreate", action="store_true",
                    help="Upsert into the existing collection instead of recreating.")
    args = ap.parse_args()

    rows = _load_rows(args.table)
    if not rows:
        raise RuntimeError(f"No rows in {args.table} — run scripts/build_eu_global_dataset.py first")
    print(f"loaded_rows={len(rows)} from {args.table}")

    client = get_chroma_client()
    if not args.no_recreate:
        try:
            client.delete_collection(args.collection)
            print(f"deleted_collection={args.collection}")
        except Exception:
            pass
        col = client.create_collection(
            name=args.collection,
            metadata={"hnsw:space": "cosine"},  # match USDA/Irish/Hungarian
        )
    else:
        col = client.get_or_create_collection(
            name=args.collection,
            metadata={"hnsw:space": "cosine"},
        )

    total = len(rows)
    upserted = 0
    for i in range(0, total, args.batch_size):
        batch = rows[i:i + args.batch_size]
        ids = [r["id"] for r in batch]
        docs = [r["food_name"] for r in batch]
        embs = get_embeddings_batch(docs)
        metas = [
            {
                "eu_id": r["id"],
                "food_name": r["food_name"],
                "title": r["food_name"],   # keep parity with Hungarian builder's "title"
                "source": r["source"],
                "country": r["country"],
                "food_group": r["food_group"] or "",
            }
            for r in batch
        ]
        col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
        upserted += len(batch)
        if upserted % (args.batch_size * 4) == 0 or upserted == total:
            print(f"progress={upserted}/{total}")

    print(f"collection={args.collection} final_count={col.count()}")


if __name__ == "__main__":
    main()
