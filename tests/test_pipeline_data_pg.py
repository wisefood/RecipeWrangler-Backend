import json

import pytest

from recipe_wrangler.utils import pipeline_data_pg as pdp


def test_postgres_entry_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"from": "file"}))
    monkeypatch.setattr(pdp, "load_pipeline_data", lambda name: {"from": "pg"})
    assert pdp.load_pipeline_data_or_file("x", path) == {"from": "pg"}


def test_file_is_read_and_logged_when_postgres_lacks_the_entry(tmp_path, monkeypatch, caplog):
    def no_pg(name):
        raise KeyError(name)

    monkeypatch.setattr(pdp, "load_pipeline_data", no_pg)
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n\n{"a": 2}\n')
    assert pdp.load_pipeline_data_or_file("x", path, jsonl=True) == [{"a": 1}, {"a": 2}]
    assert "unavailable" in caplog.text


@pytest.mark.parametrize("jsonl,empty", [(False, {}), (True, [])])
def test_missing_file_gives_an_empty_value(tmp_path, monkeypatch, jsonl, empty):
    monkeypatch.setattr(pdp, "load_pipeline_data", lambda name: (_ for _ in ()).throw(KeyError(name)))
    assert pdp.load_pipeline_data_or_file("x", tmp_path / "nope", jsonl=jsonl) == empty
