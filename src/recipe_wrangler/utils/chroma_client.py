# Purpose: Shared Chroma HTTP client configured via env vars.

import os

# Disable Chroma telemetry to avoid noisy ClientStartEvent capture errors.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "FALSE")
os.environ.setdefault("CHROMA_TELEMETRY", "FALSE")

import chromadb

# Default connection to the Dockerized Chroma server; override via env vars if needed.
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))


_client: chromadb.ClientAPI | None = None


def get_chroma_client() -> chromadb.ClientAPI:
    """Return a cached HTTP client pointed at the shared Chroma server."""
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    return _client
