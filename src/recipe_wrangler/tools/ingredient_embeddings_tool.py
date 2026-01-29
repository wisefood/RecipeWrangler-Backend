# Purpose: Ensure ingredient embeddings exist in Chroma and upsert missing ones.

from typing import List, Dict, Any, Tuple
from langchain.tools import tool
import os

# Disable Chroma telemetry before importing chromadb.
os.environ.setdefault("CHROMA_TELEMETRY", "FALSE")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")

import chromadb
import time
from pathlib import Path

from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.chroma_client import get_chroma_client

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PERSIST_PATH = REPO_ROOT / "chroma_db"
DEFAULT_COLLECTION = "ingredients"
BATCH_SIZE = 128  # upsert in chunks to reduce round-trips


def _get_or_create_collection(client: chromadb.ClientAPI, name: str):
    try:
        return client.get_collection(name)
    except Exception:
        return client.create_collection(name=name)


def _batched(items: List[Any], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _find_existing_by_name(col, names: list[str]) -> dict[str, tuple[str, dict]]:
    """
    Return a mapping: ingredient_name -> (id, metadata)
    Uses metadata.name to locate existing items.
    """
    existing: dict[str, tuple[str, dict]] = {}

    res = col.get(
        where={"name": {"$in": names}},
        include=["metadatas", "documents"],  # <-- drop "ids"
    )
    ids = res.get("ids", [])                # ids are still present in the response
    metas = res.get("metadatas", [])
    docs = res.get("documents", [])
    for _id, meta, doc in zip(ids, metas, docs):
        key = (meta or {}).get("name") or doc
        if key:
            existing[key] = (_id, meta or {})
    return existing


@tool
def ensure_ingredients_in_collection(
    ingredient_names: List[str],
    state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Ensure all provided ingredient names exist in the Chroma 'ingredients' collection.
    - Checks for existing entries (by metadata.name) without computing embeddings.
    - Embeds ONLY the missing names using get_embeddings and upserts them.
    - Returns a report of found/created/failed.

    Args:
        ingredient_names: list of raw ingredient strings.
        state: optional dict with:
            - persist_path: str (default "chroma_db")
            - collection_name: str (default "ingredients")
            - debug: bool

    Returns:
        {
          "persist_path": str,
          "collection": str,
          "found": [str, ...],
          "created": [{"name": str, "id": str}, ...],
          "failed": [str, ...],
          "total_in_collection_after": int
        }
    """
    state = state or {}
    persist_path = state.get("persist_path", DEFAULT_PERSIST_PATH)
    collection_name = state.get("collection_name", DEFAULT_COLLECTION)
    debug = bool(state.get("debug", False))

    if debug:
        print(f"[ensure_ingredients_in_collection] persist_path={persist_path} collection={collection_name}")
        print(f"[input] {len(ingredient_names)} names")

    client = get_chroma_client()
    col = _get_or_create_collection(client, collection_name)

    names = [n for n in ingredient_names if n and isinstance(n, str)]
    existing_map = _find_existing_by_name(col, names)
    found = sorted(existing_map.keys())
    missing = [n for n in names if n not in existing_map]

    if debug:
        print(f"[existing] found={len(found)} missing={len(missing)}")

    created: List[Dict[str, str]] = []
    failed: List[str] = []

    for batch in _batched(missing, BATCH_SIZE):
        if not batch:
            continue

        embeddings = []
        ok_names = []
        for name in batch:
            try:
                vec = get_embeddings(name)
                if not isinstance(vec, list):
                    raise ValueError("get_embeddings must return List[float]")
                embeddings.append(vec)
                ok_names.append(name)
            except Exception as e:
                if debug:
                    print(f"[embed-fail] {name}: {e}")
                failed.append(name)

        if not ok_names:
            continue

        # Use the raw name as the ID; we already checked for existing entries to avoid duplicates.
        ids = ok_names
        metadatas = [{"type": "ingredient", "name": n} for n in ok_names]

        try:
            col.upsert(ids=ids, documents=ok_names, embeddings=embeddings, metadatas=metadatas)
            for n, _id in zip(ok_names, ids):
                created.append({"name": n, "id": _id})
            if debug:
                print(f"[upsert] +{len(ok_names)}")
        except Exception as e:
            if debug:
                print(f"[upsert-fail] batch of {len(ok_names)}: {e}")
            failed.extend(ok_names)

    total_after = col.count()

    out = {
        "persist_path": persist_path,
        "collection": collection_name,
        "found": found,
        "created": created,
        "failed": failed,
        "total_in_collection_after": total_after,
    }

    if debug:
        print(f"[done] found={len(found)} created={len(created)} failed={len(failed)} total={total_after}")
        return out

    return out
