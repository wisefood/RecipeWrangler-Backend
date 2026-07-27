"""Elasticsearch-backed vector lookup for ingredient matching."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Sequence

from recipe_wrangler.repositories.elasticsearch_vectors import (
    elastic_hybrid_pool_size,
    get_elasticsearch_vector_collection_page,
    query_elasticsearch_hybrid_collection,
    query_elasticsearch_vector_collection_by_embedding,
)


def query_vector_collection_by_embedding(
    collection_name: str,
    embedding: Iterable[float],
    n_results: int,
) -> list[dict]:
    return query_elasticsearch_vector_collection_by_embedding(
        collection_name,
        embedding,
        n_results,
    )


def query_vector_collection(
    collection_name: str,
    query: str,
    n_results: int,
) -> list[dict]:
    return query_elasticsearch_hybrid_collection(
        collection_name,
        query,
        pool_size=max(int(n_results), elastic_hybrid_pool_size()),
    )


def get_vector_collection_page(
    collection_name: str,
    limit: int,
    offset: int,
) -> list[dict]:
    return get_elasticsearch_vector_collection_page(
        collection_name,
        limit,
        offset,
    )


@lru_cache(maxsize=4096)
def query_irish_nutrition_candidates(name: str) -> list[dict]:
    return query_vector_collection("nutritional_ingredients_irish", name, 10)


@lru_cache(maxsize=4096)
def query_usda_nutrition_candidates(name: str) -> list[dict]:
    return query_vector_collection("nutritional_ingredients_usda", name, 10)


@lru_cache(maxsize=4096)
def query_hungarian_nutrition_candidates(name: str) -> list[dict]:
    return query_vector_collection("nutritional_ingredients_hungarian", name, 10)


@lru_cache(maxsize=4096)
def query_eu_nutrition_candidates(name: str) -> list[dict]:
    return query_vector_collection("nutritional_ingredients_eu", name, 10)


@lru_cache(maxsize=4096)
def query_sustainability_candidates(name: str) -> list[dict]:
    return query_vector_collection("sustainability_ingredients", name, 5)


class VectorCollection:
    """Small collection façade used by weight matching and lexical reranking."""

    def __init__(self, name: str):
        self.name = name

    def query(
        self,
        *,
        query_embeddings: Sequence[Sequence[float]],
        n_results: int,
        include: Sequence[str] | None = None,
    ) -> dict:
        del include
        if not query_embeddings:
            return {
                "ids": [[]],
                "documents": [[]],
                "metadatas": [[]],
                "distances": [[]],
            }
        hits = query_vector_collection_by_embedding(
            self.name,
            query_embeddings[0],
            n_results,
        )
        return {
            "ids": [[hit.get("id") for hit in hits]],
            "documents": [[hit.get("document") for hit in hits]],
            "metadatas": [[hit.get("metadata") or {} for hit in hits]],
            "distances": [[hit.get("distance") for hit in hits]],
        }

    def get(
        self,
        *,
        limit: int,
        offset: int,
        include: Sequence[str] | None = None,
    ) -> dict:
        del include
        hits = get_vector_collection_page(self.name, limit, offset)
        return {
            "ids": [hit.get("id") for hit in hits],
            "documents": [hit.get("document") for hit in hits],
            "metadatas": [hit.get("metadata") or {} for hit in hits],
        }
