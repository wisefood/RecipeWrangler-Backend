"""Elasticsearch repository for dense-vector ingredient lookup."""

from __future__ import annotations

import json
import math
import os
from typing import Iterable

import requests

from recipe_wrangler.utils.get_embeddings import get_embeddings


DEFAULT_VECTOR_INDEX = "ingredient_vectors"


def elastic_vector_url() -> str:
    return os.getenv("ELASTIC_URL", "http://localhost:9200").rstrip("/")


def elastic_vector_index() -> str:
    return os.getenv("ELASTIC_VECTOR_INDEX", DEFAULT_VECTOR_INDEX).strip()


def elastic_vector_timeout() -> float:
    return float(os.getenv("ELASTIC_VECTOR_TIMEOUT", os.getenv("ELASTIC_TIMEOUT", "3.0")))


def elastic_vector_search_mode() -> str:
    mode = os.getenv("ELASTIC_VECTOR_SEARCH_MODE", "exact").strip().lower()
    if mode not in {"exact", "knn"}:
        raise ValueError("ELASTIC_VECTOR_SEARCH_MODE must be 'exact' or 'knn'")
    return mode


def elastic_hybrid_pool_size() -> int:
    """Number of candidates retrieved independently by each hybrid search arm."""
    return max(1, int(os.getenv("ELASTIC_HYBRID_POOL_SIZE", "25")))


def elastic_rrf_rank_constant() -> int:
    """RRF rank constant used to combine vector and BM25 candidate rankings."""
    return max(1, int(os.getenv("ELASTIC_HYBRID_RRF_CONSTANT", "60")))


def elastic_score_to_cosine_similarity(score: float) -> float:
    """Convert Elasticsearch's cosine kNN score back to raw cosine similarity."""
    return max(-1.0, min(1.0, 2.0 * float(score) - 1.0))


def elastic_score_to_cosine_distance(score: float) -> float:
    """Convert Elasticsearch's normalized score to cosine distance."""
    return max(0.0, min(2.0, 2.0 * (1.0 - float(score))))


def _validate_embedding(embedding: Iterable[float]) -> list[float]:
    vector = [float(value) for value in embedding]
    if not vector:
        raise ValueError("Query embedding must not be empty.")
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("Query embedding contains a non-finite value.")
    if not any(value != 0.0 for value in vector):
        raise ValueError("Cosine similarity does not support zero-magnitude vectors.")
    return vector


def _vector_search_payload(vector: list[float], result_count: int) -> dict:
    payload: dict = {
        "size": result_count,
        "_source": {"excludes": ["embedding"]},
    }
    if elastic_vector_search_mode() == "exact":
        payload["query"] = {
            "script_score": {
                "query": {"term": {"collection": "__COLLECTION__"}},
                "script": {
                    "source": "(cosineSimilarity(params.query_vector, 'embedding') + 1.0) / 2.0",
                    "params": {"query_vector": vector},
                },
            }
        }
    else:
        candidate_multiplier = max(
            1, int(os.getenv("ELASTIC_VECTOR_CANDIDATE_MULTIPLIER", "10"))
        )
        num_candidates = min(
            10_000,
            max(result_count, result_count * candidate_multiplier),
        )
        payload["knn"] = {
            "field": "embedding",
            "query_vector": vector,
            "k": result_count,
            "num_candidates": num_candidates,
            "filter": {"term": {"collection": "__COLLECTION__"}},
        }
    return payload


def _set_collection_filter(payload: dict, collection_name: str) -> dict:
    if "knn" in payload:
        payload["knn"]["filter"]["term"]["collection"] = collection_name
    else:
        payload["query"]["script_score"]["query"]["term"]["collection"] = (
            collection_name
        )
    return payload


def _source_hit(hit: dict, *, distance: float) -> dict:
    source = hit.get("_source") or {}
    return {
        "id": source.get("source_id") or hit.get("_id"),
        "document": source.get("document"),
        "metadata": source.get("metadata") or {},
        "distance": distance,
    }


def _cosine_distance(left: list[float], right: Iterable[float]) -> float:
    right_vector = [float(value) for value in right]
    if len(left) != len(right_vector):
        raise ValueError("Stored and query embeddings have different dimensions.")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right_vector))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("Cosine similarity does not support zero-magnitude vectors.")
    similarity = sum(a * b for a, b in zip(left, right_vector)) / (
        left_norm * right_norm
    )
    return max(0.0, min(2.0, 1.0 - similarity))


def query_elasticsearch_vector_collection_by_embedding(
    collection_name: str,
    embedding: Iterable[float],
    n_results: int,
) -> list[dict]:
    """Return Elasticsearch vector hits with cosine distance values."""
    result_count = max(1, int(n_results))
    vector = _validate_embedding(embedding)
    payload = _set_collection_filter(
        _vector_search_payload(vector, result_count),
        collection_name,
    )
    response = requests.post(
        f"{elastic_vector_url()}/{elastic_vector_index()}/_search",
        json=payload,
        timeout=elastic_vector_timeout(),
    )
    response.raise_for_status()

    hits: list[dict] = []
    for hit in response.json().get("hits", {}).get("hits", []):
        score = hit.get("_score")
        if score is None:
            continue
        hits.append(
            _source_hit(
                hit,
                distance=elastic_score_to_cosine_distance(float(score)),
            )
        )
    return hits


def query_elasticsearch_hybrid_collection(
    collection_name: str,
    query: str,
    *,
    pool_size: int | None = None,
) -> list[dict]:
    """Union independent vector and BM25 pools and order them with RRF."""
    result_count = max(1, int(pool_size or elastic_hybrid_pool_size()))
    vector = _validate_embedding(get_embeddings(query))
    vector_payload = _set_collection_filter(
        _vector_search_payload(vector, result_count),
        collection_name,
    )
    lexical_payload = {
        "size": result_count,
        "_source": ["source_id", "document", "metadata", "embedding"],
        "query": {
            "bool": {
                "filter": [{"term": {"collection": collection_name}}],
                "must": [
                    {
                        "bool": {
                            "should": [
                                {
                                    "match_phrase": {
                                        "document": {
                                            "query": query,
                                            "boost": 2.0,
                                        }
                                    }
                                },
                                {
                                    "match": {
                                        "document": {
                                            "query": query,
                                            "operator": "or",
                                        }
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                ],
            }
        },
    }
    body = "\n".join(
        (
            json.dumps({}),
            json.dumps(vector_payload),
            json.dumps({}),
            json.dumps(lexical_payload),
            "",
        )
    )
    response = requests.post(
        f"{elastic_vector_url()}/{elastic_vector_index()}/_msearch",
        data=body,
        headers={"Content-Type": "application/x-ndjson"},
        timeout=max(10.0, elastic_vector_timeout()),
    )
    response.raise_for_status()
    search_responses = response.json().get("responses") or []
    if len(search_responses) != 2:
        raise RuntimeError("Elasticsearch hybrid search returned an invalid response.")
    for search_response in search_responses:
        if search_response.get("error"):
            raise RuntimeError(
                f"Elasticsearch hybrid search failed: {search_response['error']}"
            )

    vector_hits = search_responses[0].get("hits", {}).get("hits", [])
    lexical_hits = search_responses[1].get("hits", {}).get("hits", [])
    merged: dict[str, dict] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        score = hit.get("_score")
        if score is None:
            continue
        item = _source_hit(
            hit,
            distance=elastic_score_to_cosine_distance(float(score)),
        )
        item["vector_rank"] = rank
        merged[str(item["id"])] = item

    for rank, hit in enumerate(lexical_hits, start=1):
        source = hit.get("_source") or {}
        embedding = source.get("embedding")
        if not isinstance(embedding, list):
            continue
        item_id = str(source.get("source_id") or hit.get("_id"))
        item = merged.get(item_id)
        if item is None:
            item = _source_hit(
                hit,
                distance=_cosine_distance(vector, embedding),
            )
            merged[item_id] = item
        item["lexical_rank"] = rank
        item["lexical_score"] = float(hit.get("_score") or 0.0)

    rank_constant = elastic_rrf_rank_constant()
    for item in merged.values():
        rrf_score = 0.0
        if item.get("vector_rank") is not None:
            rrf_score += 1.0 / (rank_constant + int(item["vector_rank"]))
        if item.get("lexical_rank") is not None:
            rrf_score += 1.0 / (rank_constant + int(item["lexical_rank"]))
        item["rrf_score"] = rrf_score

    return sorted(
        merged.values(),
        key=lambda item: (
            -float(item.get("rrf_score") or 0.0),
            (
                float(item["distance"])
                if item.get("distance") is not None
                else 2.0
            ),
        ),
    )


def query_elasticsearch_vector_collection(
    collection_name: str,
    query: str,
    n_results: int,
) -> list[dict]:
    result_count = max(1, int(n_results))
    return query_elasticsearch_hybrid_collection(
        collection_name,
        query,
        pool_size=max(result_count, elastic_hybrid_pool_size()),
    )[:result_count]


def get_elasticsearch_vector_collection_page(
    collection_name: str,
    limit: int,
    offset: int,
) -> list[dict]:
    """Fetch a deterministic page for legacy lexical reranking."""
    payload = {
        "size": max(1, int(limit)),
        "from": max(0, int(offset)),
        "_source": ["source_id", "document", "metadata"],
        "query": {"term": {"collection": collection_name}},
        "sort": [{"_doc": "asc"}],
    }
    response = requests.post(
        f"{elastic_vector_url()}/{elastic_vector_index()}/_search",
        json=payload,
        timeout=max(10.0, elastic_vector_timeout()),
    )
    response.raise_for_status()
    return [
        {
            "id": (hit.get("_source") or {}).get("source_id") or hit.get("_id"),
            "document": (hit.get("_source") or {}).get("document"),
            "metadata": (hit.get("_source") or {}).get("metadata") or {},
        }
        for hit in response.json().get("hits", {}).get("hits", [])
    ]
