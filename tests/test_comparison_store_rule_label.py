from recipe_wrangler.services.adaptation.comparison_store import _without_rules


def test_rule_label_ignored_but_content_kept():
    a = {"evidence": [{"quote": "Tuna", "rule": "scoped_anatomy_v1"}], "v": 1}
    b = {"evidence": [{"quote": "Tuna", "rule": "scoped_anatomy_v2"}], "v": 1}
    assert _without_rules(a) == _without_rules(b)
    assert _without_rules(a) != _without_rules({**b, "v": 2})


def test_recovery_version_label_ignored():
    a = {"label_evidence_recovery": [{"value": "tuna", "version": "scoped_anatomy_v1"}]}
    b = {"label_evidence_recovery": [{"value": "tuna", "version": "scoped_anatomy_v2"}]}
    assert _without_rules(a) == _without_rules(b)
    assert _without_rules(a) != _without_rules({"label_evidence_recovery": [{"value": "cod", "version": "scoped_anatomy_v1"}]})


def test_parser_version_and_tuple_vs_list_ignored():
    a = {"text_annotation": {"parser_version": "v9", "neighbours": (), "main": "pork"}}
    b = {"text_annotation": {"parser_version": "v8", "neighbours": [], "main": "pork"}}
    assert _without_rules(a) == _without_rules(b)
    assert _without_rules(a) != _without_rules({"text_annotation": {"main": "beef", "neighbours": []}})
