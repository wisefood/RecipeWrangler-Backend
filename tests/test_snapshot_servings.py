import pytest

from scripts.maintenance.sync_parsed_snapshot_to_graph import _servings


@pytest.mark.parametrize("text,expected", [("1 loaf (16 slices)", 16.0), ("Serves 4", 4.0), ("12 pancakes", 12.0), (None, None)])
def test_servings(text, expected):
    assert _servings(text) == expected
