# utils/get_qwen_embeddings.py

from functools import lru_cache
from typing import List
from langchain_huggingface import HuggingFaceEmbeddings

MODEL_NAME = "Qwen/Qwen3-Embedding-8B"
MODEL_KWARGS = {"device": "cuda"}   # or "cpu" if you prefer
ENCODE_KWARGS = {"batch_size": 8}   # tune this to fit GPU memory

@lru_cache(maxsize=1)
def _get_embedder() -> HuggingFaceEmbeddings:
    """Load the embedding model once and cache it."""
    return HuggingFaceEmbeddings(
        model_name=MODEL_NAME,
        model_kwargs=MODEL_KWARGS,
        encode_kwargs=ENCODE_KWARGS,
    )

def get_qwen_embeddings(text: str) -> List[float]:
    """Embed a single string using the cached model."""
    return _get_embedder().embed_query(text or "")

def get_qwen_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """Embed a list of strings efficiently."""
    return _get_embedder().embed_documents([t or "" for t in texts])
