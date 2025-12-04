from functools import lru_cache
from typing import List, Optional
from langchain_huggingface import HuggingFaceEmbeddings

MODEL_NAME = "Qwen/Qwen3-Embedding-8B"
MODEL_KWARGS = {"device": "cuda"}   # or "cpu" if you prefer
ENCODE_KWARGS = {"batch_size": 8}   # tune this to fit GPU memory

@lru_cache(maxsize=None)
def _get_embedder(model_name: str = MODEL_NAME) -> HuggingFaceEmbeddings:
    """
    Load the embedding model once and cache it (keyed by model name).
    """
    return HuggingFaceEmbeddings(
        model_name=model_name or MODEL_NAME,
        model_kwargs=MODEL_KWARGS,
        encode_kwargs=ENCODE_KWARGS,
    )

def get_embeddings(text: str, model_name: Optional[str] = None) -> List[float]:
    """
    Embed a single string using the cached model; override model_name to switch models.
    """
    return _get_embedder(model_name or MODEL_NAME).embed_query(text or "")

def get_embeddings_batch(texts: List[str], model_name: Optional[str] = None) -> List[List[float]]:
    """
    Embed a list of strings efficiently; override model_name to switch models.
    Default model: Qwen3-Embedding-8B
    """
    return _get_embedder(model_name or MODEL_NAME).embed_documents([t or "" for t in texts])
