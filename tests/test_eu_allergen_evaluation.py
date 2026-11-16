"""External evaluation must count missed labels and possible evidence correctly."""
import pytest

from scripts.eval.evaluate_eu_allergens import LABEL_MAP, score, strings


def test_multilabel_precision_and_recall_count_extra_and_missed_labels():
    row = {"primary": ["milk", "eggs"], "raw": {"confirmed": ["milk", "soy"], "possible": []}}
    result = score([row], "raw", "primary", False)
    assert result["micro"]["tp"] == result["micro"]["fp"] == result["micro"]["fn"] == 1
    assert result["micro"]["precision"] == result["micro"]["recall"] == result["micro"]["f1"] == .5
    assert result["exact_set_matches"] == 0
    assert result["per_label"]["eggs"]["recall"] == 0


def test_possible_evidence_is_not_counted_as_confirmed():
    row = {"primary": ["milk"], "broad": ["milk", "soy"],
           "raw": {"confirmed": ["milk"], "possible": ["milk", "soy"]}}
    assert score([row], "raw", "primary", False)["exact_set_matches"] == 1
    broad = score([row], "raw", "broad", True)
    assert broad["exact_set_matches"] == 1
    assert broad["micro"]["tp"] == 2


def test_no_predictions_on_positive_reference_counts_as_miss():
    row = {"primary": ["lupin"], "raw": {"confirmed": [], "possible": []}}
    result = score([row], "raw", "primary", False)
    assert result["micro"]["fn"] == 1
    assert result["micro"]["recall"] == 0
    assert result["micro"]["precision"] is None


def test_empty_subset_has_no_fabricated_agreement():
    result = score([], "raw", "primary", False)
    assert result["exact_set_agreement"] is None
    assert result["macro_f1_supported_labels"] is None


def test_wheat_is_not_automatically_relabelled_as_gluten():
    assert "wheat" not in LABEL_MAP
    assert len(set(LABEL_MAP.values())) == 14


@pytest.mark.parametrize("value", ["['milk', 2]", "{'milk': 1}", "__import__('os').getcwd()"])
def test_reference_lists_are_parsed_without_executing_input(value):
    with pytest.raises((ValueError, SyntaxError)):
        strings(value)
