import json
from types import SimpleNamespace

from scripts.run_openrouter_weight_review import _request_results, _signature_id, _validate
from scripts.review_openrouter_weight_candidates import _independent_reference


def test_weight_review_validation_and_signature_are_deterministic() -> None:
    signature = _signature_id("Oil", "", "missing_quantity")
    assert signature == _signature_id("oil", "", "missing_quantity")

    valid = {
        "decision": "estimate",
        "quantity": 2,
        "grams_per_unit": 30,
        "total_grams": 60,
        "min_total_grams": 40,
        "max_total_grams": 80,
        "confidence": 0.7,
    }
    assert _validate(valid) == ("valid_pending_review", "ok")
    assert _validate({**valid, "total_grams": 70})[0] == "invalid"
    assert _validate(
        {
            "decision": "abstain",
            "quantity": None,
            "grams_per_unit": None,
            "total_grams": None,
            "min_total_grams": None,
            "max_total_grams": None,
            "confidence": 0.9,
        }
    ) == ("abstained", "model_abstained")


def test_failed_batch_splits_into_single_requests() -> None:
    class Completions:
        def create(self, **kwargs):
            chunk = json.loads(kwargs["messages"][1]["content"])
            content = "{" if len(chunk) > 1 else json.dumps({"results": chunk})
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=None,
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    chunk = [{"signature_id": "one"}, {"signature_id": "two"}]
    results, input_tokens, output_tokens = _request_results(client, "model", chunk, 0)

    assert set(results) == {"one", "two"}
    assert (input_tokens, output_tokens) == (0, 0)


def test_semantic_review_requires_independent_non_leaking_evidence() -> None:
    candidate = {
        "validation_status": "valid_pending_review",
        "result": {"total_grams": 60},
    }
    bread = {
        "ingredient": "bread",
        "measurement": "2 slices",
        "occurrences": 3,
        "examples": [{"display": "2 slices", "note": ""}],
    }
    explicit = {
        "ingredient": "fish",
        "measurement": "1 large",
        "occurrences": 1,
        "examples": [{"display": "1 large", "note": "about 60 g"}],
    }
    leaked = {**explicit, "occurrences": 2}

    assert _independent_reference(bread, candidate) == (
        30.0,
        "curated_portion:bread slice",
    )
    assert _independent_reference(explicit, candidate) == (
        60.0,
        "source_explicit_mass",
    )
    assert _independent_reference(leaked, candidate) is None
