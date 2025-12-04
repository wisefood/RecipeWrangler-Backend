"""
Seed the Chroma 'common_units' collection from a small CSV.
Embeddings use sentence-transformers/all-MiniLM-L6-v2 (dim=384) via get_embeddings.
"""

from pathlib import Path
import csv
from typing import Dict, Any, List

from recipe_wrangler.utils.chroma_client import get_chroma_client, CHROMA_HOST, CHROMA_PORT
from recipe_wrangler.utils.get_embeddings import get_embeddings


HERE = Path(__file__).resolve().parent
CSV_PATH = HERE.parent / "data" / "common_units.csv"
COLLECTION_NAME = "common_units"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def load_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def to_meta(row: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for key in ("original_id", "unit", "symbol", "volume_ml", "document"):
        val = row.get(key)
        if val is None or val == "":
            continue
        if key == "volume_ml":
            try:
                val = float(val)
            except Exception:
                continue
        meta[key] = val
    return meta


def main():
    rows = load_rows(CSV_PATH)
    if not rows:
        raise SystemExit(f"No rows found in {CSV_PATH}")

    client = get_chroma_client()
    collection = client.get_or_create_collection(name=COLLECTION_NAME)

    documents = [r["document"] for r in rows]
    ids = [r["original_id"] for r in rows]
    metadatas = [to_meta(r) for r in rows]

    embeddings = [
        get_embeddings(doc, model_name=MODEL_NAME) for doc in documents
    ]

    collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    print(f"[done] Upserted {len(rows)} rows into '{COLLECTION_NAME}' at {CHROMA_HOST}:{CHROMA_PORT}")
    print(f"[path] {CSV_PATH}")


if __name__ == "__main__":
    main()
