"""Vector preparation preserves references and never asserts food equivalence."""

import json

import pytest

from scripts.sustainability import prepare_embeddings as prep


def test_same_name_keeps_source_and_market_references_separate():
    def document(identity, market):
        return {"reference_id": identity, "release": "fixture", "calculation_eligible": False,
                "reference": {"reference_id": identity, "name": "Garlic, fresh", "market": market,
                              "source_record_id": "11000", "metrics": {"climate_change": {"value": 0.383}}}}
    vector = [0.1] * prep.DIMENSIONS
    first, second = document("one", "FR"), document("two", "DK")
    rows = [prep.vector_document(doc, vector) for doc in (first, second)]
    assert rows[0]["_id"] != rows[1]["_id"]
    for doc, row in zip((first, second), rows):
        assert row["_source"]["document"] == "Garlic, fresh"
        assert row["_source"]["metadata"]["reference"] == doc["reference"]
        assert row["_source"]["metadata"]["candidate_only"] is True
        assert row["_source"]["metadata"]["calculation_eligible"] is False
        assert row["_source"]["metadata"]["facet_annotation_status"] == "pending_shared_matcher"


@pytest.mark.parametrize("vectors,count", [([], 1), ([[0.1]], 1),
    ([[float("nan")] * prep.DIMENSIONS], 1), ([[float("inf")] * prep.DIMENSIONS], 1),
    ([[0.0] * prep.DIMENSIONS], 1)])
def test_invalid_vectors_are_rejected(vectors, count):
    with pytest.raises(ValueError):
        prep.validate_vectors(vectors, count)


def test_modified_reference_export_is_rejected(tmp_path):
    (tmp_path / "references.jsonl").write_text('{}\n')
    (tmp_path / "manifest.json").write_text(json.dumps({"documents_sha256": "wrong"}))
    with pytest.raises(ValueError, match="checksum"):
        prep.load_documents(tmp_path)


def test_repeated_reference_ids_are_rejected(tmp_path):
    path = tmp_path / "references.jsonl"
    path.write_text('{"reference_id":"same"}\n' * 2)
    (tmp_path / "manifest.json").write_text(json.dumps({"documents_sha256": prep.digest(path), "references": 2}))
    with pytest.raises(ValueError, match="identity"):
        prep.load_documents(tmp_path)
