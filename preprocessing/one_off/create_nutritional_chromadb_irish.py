import ast
import uuid
from typing import List, Any, Dict

import pandas as pd
from recipe_wrangler.utils.chroma_client import get_chroma_client, CHROMA_HOST, CHROMA_PORT

# ========= USER SETTINGS =========
CSV_PATH = "data/Irish_Comp_Table.csv"
COLLECTION_NAME = "nutritional_ingredients_irish"
DISTANCE_METRIC = "cosine"        # 'cosine' | 'l2' | 'ip'
DROP_EXISTING_COLLECTION = True
DEFAULT_BATCH_SIZE = 1000
# =================================

# Columns we expect to be present
REQUIRED_COLS = ("Food Name", "Group", "embedding")  # "Description" may be present but is excluded from metadata

# Treat these as strings (do NOT coerce to float)
TEXT_COLS = {"Food Name", "Group", "Description"}
EMBED_COL = "embedding"
DESC_COL = "Description"

def parse_embedding(x: Any) -> List[float]:
    if isinstance(x, list):
        return [float(v) for v in x]
    if isinstance(x, str):
        try:
            val = ast.literal_eval(x)
        except Exception:
            s = x.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            return [float(v) for v in s.split(",") if v.strip()]
        if isinstance(val, list):
            return [float(v) for v in val]
    raise ValueError(f"Cannot parse embedding from value: {repr(x)[:120]}")

def ensure_columns(df: pd.DataFrame, required=REQUIRED_COLS):
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

def to_float_or_none(v):
    try:
        return float(v)
    except Exception:
        return None

def load_dataframe(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ensure_columns(df)

    # Normalize text cols (if present)
    for c in TEXT_COLS:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()

    # Parse embeddings
    df[EMBED_COL] = df[EMBED_COL].apply(parse_embedding)

    # Coerce all non-text, non-embedding columns to float (nutrients etc.)
    numeric_candidates = [c for c in df.columns if c not in TEXT_COLS and c != EMBED_COL]
    for c in numeric_candidates:
        df[c] = df[c].apply(to_float_or_none)

    # Drop rows missing essentials (food name + embedding)
    essentials = ["Food Name", EMBED_COL]
    df = df.dropna(subset=[c for c in essentials if c in df.columns]).reset_index(drop=True)

    # Ensure consistent embedding dimension
    dims = {len(e) for e in df[EMBED_COL]}
    if len(dims) != 1:
        raise ValueError(f"Inconsistent embedding dimensions found: {sorted(dims)}")

    return df

def upsert_in_batches(collection, ids, documents, metadatas, embeddings, initial_batch_size=DEFAULT_BATCH_SIZE):
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
            i = j
        except Exception as e:
            msg = str(e)
            if "max batch size" in msg.lower() or "greater than max batch size" in msg.lower():
                if batch == 1:
                    raise
                batch = max(1, (batch + 1) // 2)
                print(f"[warn] Reducing batch size to {batch} due to: {msg}")
            else:
                raise

def main():
    print(f"[info] Loading CSV: {CSV_PATH}")
    df = load_dataframe(CSV_PATH)
    total = len(df)
    dim = len(df.loc[0, EMBED_COL])
    print(f"[info] Rows: {total}, embedding dim: {dim}")

    print(f"[info] Connecting to Chroma HTTP at {CHROMA_HOST}:{CHROMA_PORT}")
    client = get_chroma_client()

    if DROP_EXISTING_COLLECTION:
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"[info] Deleted existing collection: {COLLECTION_NAME}")
        except Exception:
            pass

    print(f"[info] Creating collection: {COLLECTION_NAME} (metric={DISTANCE_METRIC})")
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": DISTANCE_METRIC},
    )

    # Prepare payload
    ids = [str(uuid.uuid4()) for _ in range(total)]

    # Human-readable document: just the name (no description)
    def make_doc(row: pd.Series) -> str:
        return str(row.get("Food Name", "")).strip()

    documents = [make_doc(r) for _, r in df.iterrows()]

    # Metadata: include ALL columns except embedding + description
    metadata_cols = [c for c in df.columns if c not in {EMBED_COL, DESC_COL}]
    metadatas: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        md: Dict[str, Any] = {}
        for c in metadata_cols:
            md[c] = r.get(c)
        metadatas.append(md)

    embeddings = df[EMBED_COL].tolist()

    # Safe initial batch size
    initial_batch = DEFAULT_BATCH_SIZE
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
