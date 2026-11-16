"""Scoped projection refresh and batch compatibility without live writes."""
from unittest.mock import MagicMock, Mock, patch

import pytest

from recipe_wrangler.utils import dietary_classification as classification


def test_batch_script_keeps_compatibility_exports():
    from scripts.neo4j import classify_vegan_vegetarian as script
    for name in ("_scalar", "_group_params", "_initialize_unknown", "_classify_negative",
                 "_classify_positive", "_aggregate_recipe_batch"):
        assert getattr(script, name) is getattr(classification, name)


def test_scoped_refresh_is_atomic_and_resets_before_classifying():
    from recipe_wrangler.utils import neo4j_utils
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    tx = Mock()
    tx.run.return_value.single.return_value = {"recipe_count":1, "ingredient_ids":[str(i) for i in range(101)]}
    session.execute_write.side_effect = lambda fn, rid: fn(tx, rid)
    calls = []
    with patch.object(neo4j_utils, "driver", driver), \
         patch.object(classification, "_initialize_unknown", side_effect=lambda _, g, ids:calls.append(("unknown", g, ids))), \
         patch.object(classification, "_classify_negative", side_effect=lambda _, g, ids:calls.append(("negative", g, ids))), \
         patch.object(classification, "_classify_positive", side_effect=lambda _, g, ids:calls.append(("positive", g, ids))), \
         patch.object(classification, "_aggregate_recipe_batch", return_value=1) as aggregate:
        assert classification.refresh_recipe_suitability(["r", "r"]) == 1
    assert session.execute_write.call_count == 1
    assert len(calls) == 18
    assert [c[0] for c in calls[:6]] == ["unknown", "negative", "positive"] * 2
    assert all(0 < len(c[2]) <= 100 for c in calls)
    assert aggregate.call_args_list[0].args == (tx, ["r"], "vegan")
    assert session.execute_write.call_args.args[0].timeout == 60


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_duplicate_recipe_stops_refresh(count):
    from recipe_wrangler.utils import neo4j_utils
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    tx = Mock()
    tx.run.return_value.single.return_value = {"recipe_count":count, "ingredient_ids":[]}
    session.execute_write.side_effect = lambda fn, rid: fn(tx, rid)
    with patch.object(neo4j_utils, "driver", driver), pytest.raises(ValueError, match="disappeared"):
        classification.refresh_recipe_suitability(["r"])


def test_failed_recipe_aggregation_does_not_succeed():
    from recipe_wrangler.utils import neo4j_utils
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    tx = Mock()
    tx.run.return_value.single.return_value = {"recipe_count":1, "ingredient_ids":[]}
    session.execute_write.side_effect = lambda fn, rid: fn(tx, rid)
    with patch.object(neo4j_utils, "driver", driver), \
         patch.object(classification, "_aggregate_recipe_batch", return_value=0), \
         pytest.raises(ValueError, match="did not complete"):
        classification.refresh_recipe_suitability(["r"])


def test_explicit_allergen_aliases_block_dietary_suitability():
    assert "dairy" in classification._group_params("vegan")["blocking_allergens"]
    assert "eggs" in classification._group_params("vegan")["blocking_allergens"]
    assert "milk" not in classification._group_params("vegetarian")["blocking_allergens"]


def test_empty_refresh_does_no_graph_work():
    from recipe_wrangler.utils import neo4j_utils
    with patch.object(neo4j_utils, "driver") as driver:
        assert classification.refresh_recipe_suitability([]) == 0
    driver.session.return_value.__enter__.return_value.execute_write.assert_not_called()
