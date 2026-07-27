import json
from unittest.mock import Mock, patch

import pytest

from recipe_wrangler.repositories import elasticsearch_vectors as esv
from recipe_wrangler.repositories import vector_matchers as vm


@pytest.mark.parametrize(
    ("score", "similarity", "distance"),
    [
        (1.0, 1.0, 0.0),
        (0.8, 0.6, 0.4),
        (0.5, 0.0, 1.0),
        (0.0, -1.0, 2.0),
    ],
)
def test_elasticsearch_cosine_score_conversion(score, similarity, distance):
    assert esv.elastic_score_to_cosine_similarity(score) == pytest.approx(similarity)
    assert esv.elastic_score_to_cosine_distance(score) == pytest.approx(distance)


def test_elasticsearch_query_returns_distance_hits(monkeypatch):
    monkeypatch.setenv("ELASTIC_URL", "http://elastic:9200")
    monkeypatch.setenv("ELASTIC_VECTOR_INDEX", "ingredient_vectors")
    monkeypatch.setenv("ELASTIC_VECTOR_SEARCH_MODE", "knn")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "collection::7",
                    "_score": 0.8,
                    "_source": {
                        "source_id": "7",
                        "document": "Chicken breast",
                        "metadata": {"usda_id": "05062"},
                    },
                }
            ]
        }
    }

    with patch.object(esv.requests, "post", return_value=response) as post:
        hits = esv.query_elasticsearch_vector_collection_by_embedding(
            "nutritional_ingredients_usda",
            [1.0, 0.0],
            10,
        )

    assert hits == [
        {
            "id": "7",
            "document": "Chicken breast",
            "metadata": {"usda_id": "05062"},
            "distance": pytest.approx(0.4),
        }
    ]
    payload = post.call_args.kwargs["json"]
    assert payload["knn"]["filter"] == {
        "term": {"collection": "nutritional_ingredients_usda"}
    }
    assert payload["knn"]["k"] == 10
    assert payload["knn"]["num_candidates"] == 100
    assert payload["_source"] == {"excludes": ["embedding"]}


def test_exact_search_uses_same_normalized_score_contract(monkeypatch):
    monkeypatch.setenv("ELASTIC_VECTOR_SEARCH_MODE", "exact")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"hits": {"hits": []}}

    with patch.object(esv.requests, "post", return_value=response) as post:
        esv.query_elasticsearch_vector_collection_by_embedding(
            "nutritional_ingredients_usda",
            [1.0, 0.0],
            10,
        )

    payload = post.call_args.kwargs["json"]
    script_score = payload["query"]["script_score"]
    assert script_score["query"] == {
        "term": {"collection": "nutritional_ingredients_usda"}
    }
    assert (
        script_score["script"]["source"]
        == "(cosineSimilarity(params.query_vector, 'embedding') + 1.0) / 2.0"
    )


def test_hybrid_search_unions_vector_and_bm25_pools(monkeypatch):
    monkeypatch.setenv("ELASTIC_URL", "http://elastic:9200")
    monkeypatch.setenv("ELASTIC_VECTOR_INDEX", "ingredient_vectors")
    monkeypatch.setenv("ELASTIC_VECTOR_SEARCH_MODE", "exact")
    monkeypatch.setenv("ELASTIC_HYBRID_POOL_SIZE", "25")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "responses": [
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "shared",
                            "_score": 0.9,
                            "_source": {
                                "source_id": "shared",
                                "document": "Lentils cooked",
                                "metadata": {"eu_id": "1"},
                            },
                        },
                        {
                            "_id": "vector-only",
                            "_score": 0.8,
                            "_source": {
                                "source_id": "vector-only",
                                "document": "Lentil sprouts raw",
                                "metadata": {"eu_id": "2"},
                            },
                        },
                    ]
                }
            },
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "shared",
                            "_score": 4.0,
                            "_source": {
                                "source_id": "shared",
                                "document": "Lentils cooked",
                                "metadata": {"eu_id": "1"},
                                "embedding": [1.0, 0.0],
                            },
                        },
                        {
                            "_id": "lexical-only",
                            "_score": 3.0,
                            "_source": {
                                "source_id": "lexical-only",
                                "document": "Green lentils boiled",
                                "metadata": {"eu_id": "3"},
                                "embedding": [0.0, 1.0],
                            },
                        },
                    ]
                }
            },
        ]
    }

    with patch.object(esv, "get_embeddings", return_value=[1.0, 0.0]), \
         patch.object(esv.requests, "post", return_value=response) as post:
        hits = esv.query_elasticsearch_hybrid_collection(
            "nutritional_ingredients_eu",
            "cooked green lentils",
        )

    assert [hit["id"] for hit in hits] == [
        "shared",
        "vector-only",
        "lexical-only",
    ]
    assert hits[0]["vector_rank"] == 1
    assert hits[0]["lexical_rank"] == 1
    assert hits[2]["distance"] == pytest.approx(1.0)

    assert post.call_args.args[0] == (
        "http://elastic:9200/ingredient_vectors/_msearch"
    )
    assert post.call_args.kwargs["headers"] == {
        "Content-Type": "application/x-ndjson"
    }
    lines = post.call_args.kwargs["data"].strip().splitlines()
    vector_payload = json.loads(lines[1])
    lexical_payload = json.loads(lines[3])
    assert vector_payload["size"] == 25
    assert lexical_payload["size"] == 25
    assert vector_payload["query"]["script_score"]["query"] == {
        "term": {"collection": "nutritional_ingredients_eu"}
    }
    assert lexical_payload["query"]["bool"]["filter"] == [
        {"term": {"collection": "nutritional_ingredients_eu"}}
    ]


def test_collection_page_uses_doc_order_without_id_fielddata(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"hits": {"hits": []}}

    with patch.object(esv.requests, "post", return_value=response) as post:
        assert esv.get_elasticsearch_vector_collection_page(
            "sustainability_ingredients", 5000, 0
        ) == []

    payload = post.call_args.kwargs["json"]
    assert payload["sort"] == [{"_doc": "asc"}]
    assert payload["query"] == {
        "term": {"collection": "sustainability_ingredients"}
    }


def test_elasticsearch_rejects_zero_query_vector():
    with pytest.raises(ValueError, match="zero-magnitude"):
        esv.query_elasticsearch_vector_collection_by_embedding(
            "ingredients", [0.0, 0.0], 5
        )


def test_invalid_search_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("ELASTIC_VECTOR_SEARCH_MODE", "invalid")
    with pytest.raises(ValueError, match="ELASTIC_VECTOR_SEARCH_MODE"):
        esv.query_elasticsearch_vector_collection_by_embedding(
            "ingredients", [1.0, 0.0], 5
        )


def test_vector_matcher_queries_elasticsearch_directly():
    elasticsearch_hits = [{"id": "1", "distance": 0.20001}]

    with patch.object(
        vm,
        "query_elasticsearch_vector_collection_by_embedding",
        return_value=elasticsearch_hits,
    ) as query:
        result = vm.query_vector_collection_by_embedding(
            "ingredients", [1.0, 0.0], 5
        )

    assert result == elasticsearch_hits
    query.assert_called_once_with("ingredients", [1.0, 0.0], 5)


def test_text_matcher_uses_full_hybrid_candidate_pool(monkeypatch):
    monkeypatch.setenv("ELASTIC_HYBRID_POOL_SIZE", "25")
    elasticsearch_hits = [{"id": "1", "distance": 0.2}]

    with patch.object(
        vm,
        "query_elasticsearch_hybrid_collection",
        return_value=elasticsearch_hits,
    ) as query:
        result = vm.query_vector_collection("ingredients", "red pepper", 10)

    assert result == elasticsearch_hits
    query.assert_called_once_with(
        "ingredients",
        "red pepper",
        pool_size=25,
    )


def test_vector_collection_facade_preserves_collection_shape():
    hits = [
        {
            "id": "abc",
            "document": "Rice",
            "metadata": {"usda_id": "1"},
            "distance": 0.1,
        }
    ]
    collection = vm.VectorCollection("usda")
    with patch.object(vm, "query_vector_collection_by_embedding", return_value=hits):
        result = collection.query(query_embeddings=[[1.0, 0.0]], n_results=1)

    assert result == {
        "ids": [["abc"]],
        "documents": [["Rice"]],
        "metadatas": [[{"usda_id": "1"}]],
        "distances": [[0.1]],
    }
