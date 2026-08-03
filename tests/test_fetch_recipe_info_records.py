"""The bulk recipe lookup must read what run_query actually returns.

`run_query` returns neo4j Record objects and always has. The bulk fetch
guarded each row with `isinstance(record, dict)` — false for every Record —
so it returned {} for ids that exist, the details endpoint answered 200 with
empty results, and every enrichment consumer downstream starved silently:
the weekly calorie tracker logged `Calories: 0.0/14000.0` on every step,
"provably lighter" swaps could never verify, day nutrition chips were blank,
and a member asking "how much protein is in this plan?" was told the data
was missing for every meal. One isinstance, all of it.
"""

from __future__ import annotations

from unittest.mock import patch

from recipe_wrangler.tools import fetch_recipe_info as F


class _FakeRecord:
    """Shaped like neo4j.Record for our purposes: not a dict, dict()-able."""

    def __init__(self, payload: dict):
        self._payload = payload

    def keys(self):
        return self._payload.keys()

    def __getitem__(self, key):
        return self._payload[key]

    # Deliberately NO .get and NOT a dict subclass — dict(record) must be
    # the access path, exactly as with the real driver type.


def _record(lookup_id: str, title: str) -> _FakeRecord:
    return _FakeRecord({
        "lookup_id": lookup_id,
        "recipe": {"recipe_id": lookup_id, "title": title, "serves": 2},
        "ingredients": [{"name": "salt", "quantity": None, "unit": None, "measurement": ""}],
        "tags": ["quick"],
        "dish_types": [],
    })


class TestBulkFetchReadsRecords:
    def test_records_are_not_silently_discarded(self):
        rows = [_record("r-1", "Spicy scrambled eggs"), _record("r-2", "Kid's scrambled eggs")]

        with patch.object(F, "run_query", return_value=rows):
            out = F.fetch_recipe_info_by_ids(["r-1", "r-2"])

        assert set(out) == {"r-1", "r-2"}, (
            "every Record row must land in the result — the dict guard "
            "dropped all of them and starved every enrichment consumer"
        )
        assert out["r-1"]["title"] == "Spicy scrambled eggs"

    def test_plain_dict_rows_still_work(self):
        """Some tests and callers hand dicts; both shapes must read."""
        rows = [{
            "lookup_id": "r-3",
            "recipe": {"recipe_id": "r-3", "title": "Beans"},
            "ingredients": [], "tags": [], "dish_types": [],
        }]
        with patch.object(F, "run_query", return_value=rows):
            out = F.fetch_recipe_info_by_ids(["r-3"])

        assert "r-3" in out

    def test_rows_without_lookup_id_are_skipped_not_fatal(self):
        rows = [_FakeRecord({"lookup_id": None, "recipe": {}, "ingredients": [], "tags": [], "dish_types": []})]
        with patch.object(F, "run_query", return_value=rows):
            assert F.fetch_recipe_info_by_ids(["r-x"]) == {}
