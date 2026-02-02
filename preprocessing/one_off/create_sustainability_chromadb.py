import ast
import uuid
from typing import List, Any

import pandas as pd
from recipe_wrangler.utils.chroma_client import get_chroma_client, CHROMA_HOST, CHROMA_PORT


# ========= USER SETTINGS (edit these if needed) =========
CSV_PATH = "data/SustainableFooDB_Ingredient_CF.csv"   # <-- put your CSV path here
COLLECTION_NAME = "sustainability_ingredients"     # collection name (fixed per your request)
DISTANCE_METRIC = "cosine"                         # 'cosine' | 'l2' | 'ip'
DROP_EXISTING_COLLECTION = True                    # set True to rebuild cleanly
DEFAULT_BATCH_SIZE = 1000                          # safe default under typical Chroma limits
# ========================================================


def parse_embedding(x: Any) -> List[float]:
    """
    Turn a CSV cell into a list[float].
    Accepts:
      - already-a-list
      - JSON/pythonic string like "[0.1, -0.2, ...]"
    """
    if isinstance(x, list):
        return [float(v) for v in x]
    if isinstance(x, str):
        # Try Python literal first (fast, safe for lists), then fall back to JSON-like just in case
        try:
            val = ast.literal_eval(x)
        except Exception:
            # last resort: strip and split
            s = x.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            return [float(v) for v in s.split(",") if v.strip()]
        if isinstance(val, list):
            return [float(v) for v in val]
    # If all else fails, raise a helpful error
    raise ValueError(f"Cannot parse embedding from value: {repr(x)[:120]}")


def ensure_columns(df: pd.DataFrame, required=("Ingredient", "Category", "CF_val", "embedding")):
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")


def coerce_numeric_cf(cf):
    try:
        return float(cf)
    except Exception:
        return None




def load_dataframe(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ensure_columns(df)
    # Clean up and coerce types
    df["Ingredient"] = df["Ingredient"].astype(str).str.strip()
    df["Category"] = df["Category"].astype(str).str.strip()
    df["CF_val"] = df["CF_val"].apply(coerce_numeric_cf)
    # Drop rows missing essentials
    df = df.dropna(subset=["Ingredient", "Category", "CF_val", "embedding"]).reset_index(drop=True)
    # Parse embeddings
    df["embedding"] = df["embedding"].apply(parse_embedding)
    # Ensure all embeddings have same dimensionality
    dims = {len(e) for e in df["embedding"]}
    if len(dims) != 1:
        raise ValueError(f"Inconsistent embedding dimensions found: {sorted(dims)}")
    return df


def upsert_in_batches(collection, ids, documents, metadatas, embeddings, initial_batch_size=DEFAULT_BATCH_SIZE):
    """
    Add records safely by adapting the batch size downward if the backend complains
    about exceeding max batch size.
    """
    n = len(ids)
    if n == 0:
        return

    batch = max(1, int(initial_batch_size))

    i = 0
    while i < n:
        j = min(i + batch, n)
        try:
            collection.add(
                ids=ids[i:j],
                documents=documents[i:j],
                metadatas=metadatas[i:j],
                embeddings=embeddings[i:j],
            )
            i = j  # advance on success
        except Exception as e:
            msg = str(e)
            # If the batch is larger than allowed, reduce and retry
            if "max batch size" in msg.lower() or "greater than max batch size" in msg.lower():
                if batch == 1:
                    raise  # can't reduce further
                # halve batch size (ceil)
                batch = max(1, (batch + 1) // 2)
                print(f"[warn] Reducing batch size to {batch} due to: {msg}")
            else:
                # surface other errors
                raise


def main():
    print(f"[info] Loading CSV: {CSV_PATH}")
    df = load_dataframe(CSV_PATH)
    total = len(df)
    dim = len(df.loc[0, "embedding"])
    print(f"[info] Rows: {total}, embedding dim: {dim}")

    print(f"[info] Connecting to Chroma HTTP at {CHROMA_HOST}:{CHROMA_PORT}")
    client = get_chroma_client()

    if DROP_EXISTING_COLLECTION:
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"[info] Deleted existing collection: {COLLECTION_NAME}")
        except Exception:
            # ignore if it doesn't exist
            pass

    # Create collection (get_or_create also works; we explicitly create for clarity)
    print(f"[info] Creating collection: {COLLECTION_NAME} (metric={DISTANCE_METRIC})")
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": DISTANCE_METRIC},  # for newer chromadb versions
    )

    # Prepare payload
    ids = [str(uuid.uuid4()) for _ in range(total)]
    documents = df["Ingredient"].tolist()
    metadatas = [
        {
            "ingredient": ing,
            "category": cat,
            "cf_val": float(cf) if cf is not None else None,
        }
        for ing, cat, cf in zip(df["Ingredient"], df["Category"], df["CF_val"])
    ]
    embeddings = df["embedding"].tolist()

    # Determine a safe initial batch size if the client exposes one (best-effort)
    initial_batch = DEFAULT_BATCH_SIZE
    # Older/newer chromadb may differ; this attribute may not exist — ignore if absent
    try:
        max_bs = getattr(client, "max_batch_size", None)
        if isinstance(max_bs, int) and max_bs > 0:
            initial_batch = min(initial_batch, max_bs)
    except Exception:
        pass

    print(f"[info] Upserting {total} vectors in batches (initial batch={initial_batch}) ...")
    upsert_in_batches(collection, ids, documents, metadatas, embeddings, initial_batch_size=initial_batch)

    count = collection.count()
    print(f"[done] Collection '{COLLECTION_NAME}' is ready. Vector count: {count}")


if __name__ == "__main__":
    main()
