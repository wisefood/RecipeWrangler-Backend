# Purpose: Embedding helpers (single and batch) using HuggingFace models.

import os
from functools import lru_cache
from typing import List, Optional
from langchain_huggingface import HuggingFaceEmbeddings

# Allow overriding model/device/batch via env to avoid GPU OOM.
DEFAULT_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "Qwen/Qwen3-Embedding-8B")
DEFAULT_DEVICE = os.getenv("EMBED_DEVICE", "cpu")
DEFAULT_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "8"))

MODEL_KWARGS = {"device": DEFAULT_DEVICE}
ENCODE_KWARGS = {"batch_size": DEFAULT_BATCH_SIZE}
WARM_ON_IMPORT = os.getenv("EMBED_WARM_ON_IMPORT", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

@lru_cache(maxsize=None)
def _get_embedder(model_name: str = DEFAULT_MODEL_NAME) -> HuggingFaceEmbeddings:
    """Load the embedding model once and cache it (keyed by model name)."""
    return HuggingFaceEmbeddings(
        model_name=model_name or DEFAULT_MODEL_NAME,
        model_kwargs=MODEL_KWARGS,
        encode_kwargs=ENCODE_KWARGS,
    )

# Keep import-time startup lightweight by default. Opt in to warm-up for
# environments where faster first-query latency matters more than boot time.
if WARM_ON_IMPORT:
    _get_embedder(DEFAULT_MODEL_NAME)

def get_embeddings(text: str, model_name: Optional[str] = None) -> List[float]:
    """
    Embed a single string using the cached model; override model_name to switch models.
    """
    return _get_embedder(model_name or DEFAULT_MODEL_NAME).embed_query(text or "")

def get_embeddings_batch(texts: List[str], model_name: Optional[str] = None) -> List[List[float]]:
    """
    Embed a list of strings efficiently; override model_name to switch models.
    Default model: Qwen3-Embedding-8B
    """
    return _get_embedder(model_name or DEFAULT_MODEL_NAME).embed_documents([t or "" for t in texts])
