import chromadb
from utils.get_qwen_embeddings import get_qwen_embeddings
from tools.ingredient_embeddings_tool import ensure_ingredients_in_collection
import numpy as np

PERSIST_PATH = "chroma_db"

def get_ingredient_embedding(ingredient_name: str):
    PERSIST_PATH = "chroma_db"
    client = chromadb.PersistentClient(path=PERSIST_PATH)
    collection = client.get_collection(name="ingredients")

    res = collection.get(
        where={"name": ingredient_name},
        include=["embeddings", "metadatas"]
    )

    embs = res.get("embeddings")
    if embs is None or len(embs) == 0:
        raise ValueError(f"No embedding found for {ingredient_name!r}")

    vec = embs[0]  # first embedding

    # Normalize to a flat Python list
    if isinstance(vec, np.ndarray):
        vec = vec.ravel().tolist()
    elif isinstance(vec, list) and len(vec) == 1 and isinstance(vec[0], (list, np.ndarray)):
        vec = np.asarray(vec).ravel().tolist()
    elif not isinstance(vec, list):
        vec = list(vec)

    return vec  # 1-D list[float]


def query_ingredients_db(query: str):

    COLLECTION_NAME = "ingredients"

    # connect to Chroma
    client = chromadb.PersistentClient(path=PERSIST_PATH)
    collection = client.get_collection(name=COLLECTION_NAME)

    # 1) embed query with the same model used for collection
    vec = get_qwen_embeddings(query)

    # 2) run vector similarity search
    results = collection.query(
        query_embeddings=[vec],
        n_results=5,
        include=["documents", "metadatas", "distances"]
    )

    # 3) reshape results for convenience
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
    COLLECTION_NAME = "sustainability_ingredients"

    client = chromadb.PersistentClient(path=PERSIST_PATH)
    collection = client.get_collection(name=COLLECTION_NAME)

    ensure_ingredients_in_collection.invoke({
        "ingredient_names": [query],
        "state": {"persist_path": PERSIST_PATH, "collection_name": "ingredients", "debug": False}
    })

    from utils.query_chromadb import get_ingredient_embedding
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

    # Ensure nearest-first explicitly (ascending distance)
    hits.sort(key=lambda h: h["distance"])
    return hits



def query_nutritional_db_irish(query: str):

    COLLECTION_NAME = "nutritional_ingredients_irish"

    # connect to Chroma
    client = chromadb.PersistentClient(path=PERSIST_PATH)
    collection = client.get_collection(name=COLLECTION_NAME)

    
    ensure_ingredients_in_collection.invoke({
        "ingredient_names": [query],
        "state": {"persist_path": PERSIST_PATH, "collection_name": "ingredients", "debug": False}
    })
    
    from utils.query_chromadb import get_ingredient_embedding
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
