# Purpose: Runtime Chroma query helpers for ingredients/nutrition/sustainability.

from functools import lru_cache
from pathlib import Path
import os

# Disable Chroma telemetry before importing chromadb.
os.environ.setdefault("CHROMA_TELEMETRY", "FALSE")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

import chromadb
import numpy as np
import recipe_wrangler
from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.get_embeddings import get_embeddings

REPO_ROOT = Path(recipe_wrangler.__file__).resolve().parents[2]  # package -> src -> repo
PERSIST_PATH = REPO_ROOT / "chroma_db"

def get_ingredient_embedding(ingredient_name: str):
    """
    Return query embedding for ingredient name.

    Avoid runtime lookup in Chroma `ingredients` collection. That metadata `get()`
    path can block indefinitely against remote server; direct embedding is stable
    and enough for similarity search against nutrition/sustainability collections.
    """
    vec = get_embeddings(ingredient_name)

    # Normalize to a flat Python list
    if isinstance(vec, np.ndarray):
        vec = vec.ravel().tolist()
    elif isinstance(vec, list) and len(vec) == 1 and isinstance(vec[0], (list, np.ndarray, tuple)):
        vec = np.asarray(vec).ravel().tolist()
    elif not isinstance(vec, list):
        vec = list(vec)

    return vec  # 1-D list[float]

def query_ingredients_db(query: str):
    """
    Function that queries the chromadb ingredients collection with input ingredient
    """

    COLLECTION_NAME = "ingredients"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_embeddings(query)

    results = collection.query(
        query_embeddings=[vec],
        n_results=5,
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({
            "document": doc,
            "metadata": meta,
            "distance": dist
        })
    return hits

def query_sustainability_db(query: str):
    """
    Function that queries the chromadb sustainability collection with input ingredient
    """

    COLLECTION_NAME = "sustainability_ingredients"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_ingredient_embedding(query)

    results = collection.query(
        query_embeddings=[vec],
        n_results=5,
        include=["documents", "metadatas", "distances"]
    )
    
    hits = [
        {
            "document": doc,
            "metadata": meta,
            "distance": dist,
        }
        for doc, meta, dist in zip(results["documents"][0],
                                results["metadatas"][0],
                                results["distances"][0])
    ]

    hits.sort(key=lambda h: h["distance"])
    return hits

@lru_cache(maxsize=4096)
def query_nutritional_db_irish(query: str):
    """
    Function that queries the chromadb irish nutritional collection with input ingredient
    """

    COLLECTION_NAME = "nutritional_ingredients_irish"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_ingredient_embedding(query)  # comes from "ingredients" collection

    results = collection.query(
        query_embeddings=[vec],
        n_results=10,  # fetch more to allow filtering
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({"document": doc, "metadata": meta, "distance": dist})
    return hits

@lru_cache(maxsize=4096)
def query_nutritional_db_usda(query: str):
    """
    Function that queries the chromadb USDA nutritional collection with input ingredient
    """

    COLLECTION_NAME = "nutritional_ingredients_usda"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_ingredient_embedding(query)  # comes from "ingredients" collection

    results = collection.query(
        query_embeddings=[vec],
        n_results=10,
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({"document": doc, "metadata": meta, "distance": dist})
    return hits

@lru_cache(maxsize=4096)
def query_nutritional_db_hungarian(query: str):
    """
    Function that queries the chromadb Hungarian nutritional collection with input ingredient
    """

    COLLECTION_NAME = "nutritional_ingredients_hungarian"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_ingredient_embedding(query)  # comes from "ingredients" collection

    results = collection.query(
        query_embeddings=[vec],
        n_results=10,
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({"document": doc, "metadata": meta, "distance": dist})
    return hits


@lru_cache(maxsize=4096)
def query_nutritional_db_eu(query: str):
    """
    Function that queries the chromadb EU nutritional collection (Ciqual FR +
    CoFID UK + NEVO NL composite) with input ingredient.
    """

    COLLECTION_NAME = "nutritional_ingredients_eu"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)

    vec = get_ingredient_embedding(query)

    results = collection.query(
        query_embeddings=[vec],
        n_results=10,
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({"document": doc, "metadata": meta, "distance": dist})
    return hits


def query_density_db(query: str):
    """
    Function that queries the chromadb density of foods collection with input ingredient
    """

    COLLECTION_NAME = "foods_density_v1"

    client = get_chroma_client()
    collection = client.get_collection(name=COLLECTION_NAME)


    ensure_ingredients_in_collection.invoke({
        "ingredient_names": [query],
        "state": {"persist_path": PERSIST_PATH, "collection_name": "ingredients", "debug": False}
    })

    vec = get_ingredient_embedding(query) 

    results = collection.query(
        query_embeddings=[vec],
        n_results=10,  
        include=["documents", "metadatas", "distances"]
    )

    hits = []
    for doc, meta, dist in zip(results["documents"][0],
                               results["metadatas"][0],
                               results["distances"][0]):
        hits.append({"document": doc, "metadata": meta, "distance": dist})
    return hits

def query_common_units_db(query: str):
    """
    Deprecated.

    The Chroma `common_units` collection is intentionally ignored by runtime
    weight calculation because it is too small and stale to be a reliable unit
    reference. Use the curated `unit_volume_ml_ground_truth` pipeline data
    instead.
    """
    return None
