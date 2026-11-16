"""Protect entity alignment and BIO handling in the research checkpoint probe."""

from scripts.instructions.recifine_probe import LABELS, decode_spans, normalize_mention


def test_label_order_is_not_interleaved():
    assert len(LABELS) == 21
    assert LABELS[0] == "O"
    assert LABELS[1] == "B-ACTION BY CHEF"
    assert LABELS[11] == "I-ACTION BY CHEF"


def test_offsets_preserve_original_whitespace_and_punctuation():
    text = "Simmer  red beans."
    spans = decode_spans(text, [(0, 6), (8, 11), (12, 18)],
                         ["B-ACTION BY CHEF", "B-FOOD", "I-FOOD"])
    assert spans == [{"type": "ACTION BY CHEF", "start": 0, "end": 6, "text": "Simmer"},
                     {"type": "FOOD", "start": 8, "end": 18, "text": "red beans."}]


def test_orphan_inside_tag_does_not_invent_entity():
    assert decode_spans("beans", [(0, 5)], ["I-FOOD"]) == []


def test_new_begin_tag_preserves_adjacent_separate_actions():
    spans = decode_spans("Drain rinse", [(0, 5), (6, 11)], ["B-ACTION BY CHEF"] * 2)
    assert [span["text"] for span in spans] == ["Drain", "rinse"]


def test_targeted_comparison_retains_negation_and_amount():
    assert normalize_mention("20 minutes.") == "20 minutes"
    assert normalize_mention("not boiled") != normalize_mention("boiled")
    assert normalize_mention("2 minutes") != normalize_mention("20 minutes")
