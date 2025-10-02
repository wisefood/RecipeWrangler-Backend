# tools/ensure_tags_in_collection.py
from __future__ import annotations

from typing import List, Dict, Any
from langchain.tools import tool
import chromadb
import hashlib

from utils.get_qwen_embeddings import get_qwen_embeddings


DEFAULT_PERSIST_PATH = "chroma_db"
DEFAULT_COLLECTION = "tags"
BATCH_SIZE = 128  # upsert in chunks to reduce round-trips


def _stable_id(name: str) -> str:
    """Deterministic ID from the tag name (avoids duplicate rows across runs)."""
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    return f"tag-{h}"


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
    Return a mapping: tag_name -> (id, metadata)
    Uses metadata.name to locate existing items.
    """
    existing: dict[str, tuple[str, dict]] = {}

    # Batch where-query
    try:
        res = col.get(
            where={"name": {"$in": names}},
            include=["metadatas", "documents", "embeddings"],
        )
        ids = res.get("ids", [])
        metas = res.get("metadatas", [])
        docs = res.get("documents", [])
        embeddings = res.get("embeddings", [])

        for _id, meta, doc, emb in zip(ids, metas, docs, embeddings):
            if emb is None or (hasattr(emb, "__len__") and len(emb) == 0):
                continue  # treat rows without vectors as missing
            key = (meta or {}).get("name") or doc
            if key:
                existing[key] = (_id, meta or {})
        return existing
    except Exception:
        # Fallback per-name
        for name in names:
            r = col.get(
                where={"name": name},
                include=["metadatas", "documents", "embeddings"],
            )
            if not r.get("ids"):
                continue
            emb = (r.get("embeddings") or [None])[0]
            if emb is None or (hasattr(emb, "__len__") and len(emb) == 0):
                continue
            existing[name] = (
                r["ids"][0],
                (r.get("metadatas") or [{}])[0],
            )
        return existing


@tool("ensure_tags_in_collection", return_direct=False)
def ensure_tags_in_collection(
    tag_names: List[str],
    state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Ensure all provided tag names exist in the Chroma 'tags' collection.
    - Checks for existing entries (by metadata.name) without computing embeddings.
    - Embeds ONLY the missing names using get_qwen_embeddings and upserts them.
    - Returns a report of found/created/failed.

    Args:
        tag_names: list of raw tag strings.
        state: optional dict with:
            - persist_path: str (default "chroma_db")
            - collection_name: str (default "tags")
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
        print(f"[ensure_tags_in_collection] persist_path={persist_path} collection={collection_name}")
        print(f"[input] {len(tag_names)} names")

    # 1) Connect to Chroma & fetch collection
    client = chromadb.PersistentClient(path=persist_path)
    col = _get_or_create_collection(client, collection_name)

    # 2) Check existing (no embeddings)
    names = [n for n in tag_names if n and isinstance(n, str)]
    existing_map = _find_existing_by_name(col, names)
    found = sorted(existing_map.keys())
    missing = [n for n in names if n not in existing_map]

    if debug:
        print(f"[existing] found={len(found)} missing={len(missing)}")

    created: List[Dict[str, str]] = []
    failed: List[str] = []

    # 3) Embed & upsert only the missing ones
    for batch in _batched(missing, BATCH_SIZE):
        if not batch:
            continue

        embeddings = []
        ok_names = []
        for name in batch:
            try:
                vec = get_qwen_embeddings(name)
                if not isinstance(vec, list):
                    raise ValueError("get_qwen_embeddings must return List[float]")
                embeddings.append(vec)
                ok_names.append(name)
            except Exception as e:
                if debug:
                    print(f"[embed-fail] {name}: {e}")
                failed.append(name)

        if not ok_names:
            continue

        ids = [_stable_id(n) for n in ok_names]
        metadatas = [{"type": "tag", "name": n} for n in ok_names]

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
