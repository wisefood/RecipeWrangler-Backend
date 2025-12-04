import os
import chromadb

# Default connection to the Dockerized Chroma server; override via env vars if needed.
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))


def get_chroma_client() -> chromadb.ClientAPI:
    """Return an HTTP client pointed at the shared Chroma server."""
    return chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
