#!/usr/bin/env python3
"""Add embedding similarity to recipe1m-usda canonical links JSON."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np

from recipe_wrangler.utils.chroma_client import get_chroma_client


def _norm_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _iter_collection_rows(collection, batch_size: int) -> Iterable[dict]:
    total = collection.count()
    for offset in range(0, total, batch_size):
        page = collection.get(
            limit=batch_size,
            offset=offset,
            include=["metadatas", "documents", "embeddings"],
        )
        docs = page.get("documents") or []
        metas = page.get("metadatas") or []
        embs = page.get("embeddings") or []
        for doc, meta, emb in zip(docs, metas, embs):
            yield {"document": doc, "metadata": meta or {}, "embedding": emb}


def _normalize_embedding(embedding: Optional[List[float]]) -> Optional[np.ndarray]:
    if not embedding:
        return None
    vec = np.asarray(embedding, dtype=np.float32)
    denom = float(np.linalg.norm(vec))
    if denom <= 0.0:
        return None
    return vec / denom


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich recipe1m-usda-links-canonical JSON with embedding similarity."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/mappings/recipe1m-usda-links-canonical.json"),
        help="Input links JSON path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to in-place overwrite of --input.",
    )
    parser.add_argument(
        "--recipe-collection",
        default="recipe1m_ingredients",
        help="Chroma collection containing Recipe1M ingredient embeddings.",
    )
    parser.add_argument(
        "--usda-collection",
        default="usda_ingredients_canonical",
        help="Chroma collection containing USDA canonical-link embeddings.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Batch size for Chroma paging.",
    )
    parser.add_argument(
        "--similarity-key",
        default="embedding_similarity",
        help="JSON field name for similarity score.",
    )
    args = parser.parse_args()

    input_path = args.input
    output_path = args.output or args.input

    links = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(links, list):
        raise ValueError(f"Expected JSON array at {input_path}")

    canonical_needed = {_norm_text(str(row.get("canonical") or "")) for row in links}
    canonical_needed.discard("")

    cid_to_rows: Dict[str, List[int]] = defaultdict(list)
    for idx, row in enumerate(links):
        cid = str(row.get("canonical_id") or "").strip()
        if cid:
            cid_to_rows[cid].append(idx)

    client = get_chroma_client()
    recipe_col = client.get_collection(name=args.recipe_collection)
    usda_col = client.get_collection(name=args.usda_collection)

    recipe_vecs: Dict[str, np.ndarray] = {}
    for row in _iter_collection_rows(recipe_col, batch_size=args.batch_size):
        meta = row["metadata"]
        name = _norm_text(str(meta.get("name") or row["document"] or ""))
        if not name or name not in canonical_needed or name in recipe_vecs:
            continue
        vec = _normalize_embedding(row["embedding"])
        if vec is not None:
            recipe_vecs[name] = vec

    matched = 0
    missing_recipe = 0
    missing_usda = 0

    for row in _iter_collection_rows(usda_col, batch_size=args.batch_size):
        meta = row["metadata"]
        canonical_id = str(meta.get("canonical_id") or "").strip()
        if not canonical_id or canonical_id not in cid_to_rows:
            continue

        usda_vec = _normalize_embedding(row["embedding"])
        if usda_vec is None:
            for idx in cid_to_rows[canonical_id]:
                links[idx][args.similarity_key] = None
                links[idx]["embedding_similarity_source"] = "missing_usda_embedding"
                missing_usda += 1
            continue

        for idx in cid_to_rows[canonical_id]:
            canonical_name = _norm_text(str(links[idx].get("canonical") or ""))
            recipe_vec = recipe_vecs.get(canonical_name)
            if recipe_vec is None:
                links[idx][args.similarity_key] = None
                links[idx]["embedding_similarity_source"] = "missing_recipe_embedding"
                missing_recipe += 1
                continue
            sim = float(np.dot(recipe_vec, usda_vec))
            links[idx][args.similarity_key] = sim
            links[idx]["embedding_similarity_source"] = "chroma_embeddings"
            matched += 1

    output_path.write_text(
        json.dumps(links, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"input_rows={len(links)}")
    print(f"recipe_vectors_found={len(recipe_vecs)}")
    print(f"matched={matched}")
    print(f"missing_recipe_embedding={missing_recipe}")
    print(f"missing_usda_embedding={missing_usda}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()

