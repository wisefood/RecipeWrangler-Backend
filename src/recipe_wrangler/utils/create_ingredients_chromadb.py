# build_ingredients_collection.py
# Create (or append to) a Chroma collection "ingredients" with embeddings
# for all unique, cleaned ingredients from your curated recipes CSV.

from pathlib import Path

# --- config ---
REPO_ROOT = Path(__file__).resolve().parents[3]
PERSIST_PATH = REPO_ROOT / "chroma_db"          # single folder for all your Chroma collections
COLLECTION_NAME = "ingredients"     # bump to "ingredients_v2" if you re-embed with a new model
BATCH_SIZE = 256                    # adjust based on your hardware / embed API limits
CSV_PATH = REPO_ROOT / "data/pp_recipes_preprocessed_v3_weighted_and_tagged_1000_profiled.csv"
LIMIT_FOR_TESTING = None           # set e.g. 10 while testing; None = all

# --- imports ---
from typing import Iterable, List
import pandas as pd
import ast
from tqdm import tqdm
import chromadb

# your project tools
from recipe_wrangler.tools.ingredient_cleaner import ingredient_cleaning_tool
from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.chroma_client import get_chroma_client, CHROMA_HOST, CHROMA_PORT


# ---------- helpers ----------
def batched(iterable: Iterable, batch_size: int):
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def normalize_ing(s: str) -> str:
    # safe minimal normalization (collapse whitespace). You've already cleaned via the tool.
    return " ".join((s or "").strip().split())


def get_or_create_collection(client: chromadb.ClientAPI, name: str):
    try:
        return client.get_collection(name)
    except Exception:
        return client.create_collection(name=name)


def embed_batch(texts: List[str]) -> List[List[float]]:
    """
    Simple batch wrapper around your get_embeddings (single-string).
    If you have a native batch function, replace this.
    """
    vecs: List[List[float]] = []
    for t in texts:
        v = get_embeddings(t)
        if not isinstance(v, list):
            raise ValueError("get_embeddings must return a List[float]")
        vecs.append(v)
    return vecs


def build_chroma_collection(
    unique_ingredients_cleaned: List[str],
    collection_name: str = COLLECTION_NAME,
    batch_size: int = BATCH_SIZE,
):
    client = get_chroma_client()
    col = get_or_create_collection(client, collection_name)

    existing_count = col.count()
    if existing_count:
        print(f"[info] Collection '{collection_name}' already has {existing_count} items. New items will be appended.")

    # Prepare items & ids
    items: List[str] = [normalize_ing(x) for x in unique_ingredients_cleaned]
    # Make deterministic ids from text (lower effort, works without extra deps)
    # If you plan to allow duplicates later, switch to UUIDs.
    ids: List[str] = [f"ing-{i:09d}" for i in range(existing_count, existing_count + len(items))]

    # Insert in batches
    added = 0
    for id_batch, text_batch in tqdm(
        zip(batched(ids, batch_size), batched(items, batch_size)),
        total=(len(items) + batch_size - 1) // batch_size,
        desc="Upserting to Chroma",
        unit="batch",
    ):
        id_batch = list(id_batch)
        text_batch = list(text_batch)
        if not text_batch:
            continue

        # Optional: skip texts already present by trying a 'get' per id_batch.
        # For now, we just append; re-running with the same ids will overwrite.

        embeddings = embed_batch(text_batch)  # -> List[List[float]]
        if not embeddings or not isinstance(embeddings[0], list):
            raise ValueError("embed_batch must return List[List[float]]")

        col.upsert(
            ids=id_batch,
            documents=text_batch,
            embeddings=embeddings,
            metadatas=[{"type": "ingredient", "name": txt} for txt in text_batch],
        )
        added += len(text_batch)

    print(f"[done] Upserted {added} items into collection '{collection_name}'. Final count: {col.count()}")
    print(f"[chroma] Using HTTP Chroma at {CHROMA_HOST}:{CHROMA_PORT}")


# ---------- main ----------
if __name__ == "__main__":
    # 1) Load CSV and parse ingredient lists
    df = pd.read_csv(CSV_PATH)
    # column contains list-likes as strings; convert to python lists
    df["ingredient_names"] = df["ingredient_names"].apply(ast.literal_eval)

    # 2) Collect unique raw ingredients
    unique_ingredients = set()
    for ingredients in df["ingredient_names"]:
        unique_ingredients.update(ingredients)

    unique_ingredients = sorted(unique_ingredients)
    if LIMIT_FOR_TESTING is not None:
        unique_ingredients = unique_ingredients[:LIMIT_FOR_TESTING]

    print(f"[info] Unique ingredients (raw): {len(unique_ingredients)}")

    # 3) Clean each ingredient via your tool (one by one for determinism)
    cleaned_all: List[str] = []
    for ing in tqdm(unique_ingredients, desc="Cleaning ingredients", unit="ingredient"):
        out = ingredient_cleaning_tool.invoke({"ingredient_names": [ing]})
        cleaned_values = (out or {}).get("cleaned") or []
        if cleaned_values:
            cleaned_all.append(cleaned_values[0])

    unique_ingredients_cleaned = cleaned_all
    print(f"[info] Cleaned unique ingredients: {len(unique_ingredients_cleaned)}")

    # 4) Build / append to Chroma collection
    build_chroma_collection(unique_ingredients_cleaned)
