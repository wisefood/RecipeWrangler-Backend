#!/usr/bin/env python3
"""Index SUPPLEMENTAL_FOODS (scripts/build_eu_global_dataset.py) as vector documents.

Idempotent: the document id is sha256(collection NUL source_id), the same scheme as
import_vector_embeddings.py, so re-running overwrites instead of duplicating.
Pass --ids to limit which supplemental rows are indexed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path

import requests
from sentence_transformers import SentenceTransformer

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL = "BAAI/bge-small-en-v1.5"
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
COLLECTION = "nutritional_ingredients_eu"


def _supplemental_foods() -> tuple:
    spec = importlib.util.spec_from_file_location(
        "build_eu_global_dataset", REPO_ROOT / "scripts" / "build_eu_global_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SUPPLEMENTAL_FOODS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--es-url", default=os.getenv("ELASTIC_URL", "http://localhost:9200"))
    parser.add_argument("--index", default="ingredient_vectors_v1")
    parser.add_argument("--ids", nargs="*", help="supplemental row ids to index (default: all)")
    args = parser.parse_args()

    rows = [r for r in _supplemental_foods() if not args.ids or r["id"] in args.ids]
    model = SentenceTransformer(MODEL, revision=REVISION)
    vectors = model.encode([r["food_name"] for r in rows], normalize_embeddings=True)
    for row, vector in zip(rows, vectors):
        doc_id = hashlib.sha256(f"{COLLECTION}\0{row['id']}".encode()).hexdigest()
        body = {
            "collection": COLLECTION,
            "source_id": row["id"],
            "document": row["food_name"],
            "metadata": {
                "country": row["country"],
                "eu_id": row["id"],
                "food_group": row["food_group"],
                "food_name": row["food_name"],
                "source": row["source"],
                "source_url": row["source_url"],
                "title": row["food_name"],
            },
            "embedding_model": MODEL,
            "embedding_revision": REVISION,
            "vector_space": "cosine",
            "embedding": vector.tolist(),
        }
        response = requests.put(f"{args.es_url}/{args.index}/_doc/{doc_id}", json=body, timeout=30)
        response.raise_for_status()
        print(row["id"], response.json()["result"])
    requests.post(f"{args.es_url}/{args.index}/_refresh", timeout=30).raise_for_status()


if __name__ == "__main__":
    main()
