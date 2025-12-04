import json
from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import QdrantClient, models
from recipe_wrangler.utils.chroma_client import get_chroma_client

CHROMA_COLLECTION = "nutritional_ingredients_irish"
QDRANT_COLLECTION = "nutritional-dataset-irish"
EXPORT_PATH = Path("nutritional_irish_export.jsonl")

EXPORT_BATCH = 1000
IMPORT_BATCH = 64


def export_chroma_collection() -> int:
    client = get_chroma_client()
    collection = client.get_collection(CHROMA_COLLECTION)

    total = collection.count()
    offset = 0

    with EXPORT_PATH.open("w") as f:
        while offset < total:
            batch = collection.get(
                include=["embeddings", "metadatas", "documents"],
                limit=EXPORT_BATCH,
                offset=offset,
            )

            for i in range(len(batch["ids"])):
                emb: Any = batch["embeddings"][i]
                if isinstance(emb, np.ndarray):
                    emb = emb.tolist()

                entry = {
                    "id": batch["ids"][i],
                    "embedding": emb,
                    "metadata": batch["metadatas"][i],
                    "document": batch["documents"][i],
                }
                f.write(json.dumps(entry) + "\n")

            offset += len(batch["ids"])
            print(f"[export] {offset}/{total}")

    return total


def import_to_qdrant(vector_size: int) -> None:
    client = QdrantClient(host="localhost", port=6333, timeout=60)

    client.recreate_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
    )

    points = []
    with EXPORT_PATH.open() as f:
        for line_no, line in enumerate(f, 1):
            obj = json.loads(line)
            payload = {
                "document": obj["document"],
                "metadata": obj.get("metadata") or {},
            }

            points.append(
                models.PointStruct(
                    id=obj["id"],
                    vector=obj["embedding"],
                    payload=payload,
                )
            )

            if len(points) >= IMPORT_BATCH:
                print(f"[import] uploading batch ending at line {line_no}")
                client.upsert(collection_name=QDRANT_COLLECTION, points=points)
                points = []

    if points:
        client.upsert(collection_name=QDRANT_COLLECTION, points=points)

    count = client.count(collection_name=QDRANT_COLLECTION).count
    print(f"[done] Imported {count} vectors into '{QDRANT_COLLECTION}'")


if __name__ == "__main__":
    print(f"Exporting Chroma collection '{CHROMA_COLLECTION}' ...")
    export_chroma_collection()

    # Determine vector dimension from first exported line to avoid hardcoding
    with EXPORT_PATH.open() as f:
        first = json.loads(next(f))
        dim = len(first["embedding"])

    print(f"Importing into Qdrant collection '{QDRANT_COLLECTION}' (dim={dim}) ...")
    import_to_qdrant(vector_size=dim)
