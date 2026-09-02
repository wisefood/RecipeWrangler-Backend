from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "one_off"
    / "link_cost_products_to_ingredients.py"
)
SPEC = importlib.util.spec_from_file_location("cost_graph_mapping_script", SCRIPT_PATH)
assert SPEC and SPEC.loader
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def _ingredient(name: str = "chicken wings") -> dict[str, str]:
    return {"ingredient_id": "ingredient-1", "ingredient_name": name}


def _foodon(
    product_id: str = "base__chicken",
    *,
    distance: int = 2,
    method: str = "exact_label",
) -> dict[str, object]:
    return {
        "product_id": product_id,
        "ingredient_foodon_id": "FOODON_00002674",
        "cost_anchor_foodon_id": "FOODON_00001040",
        "ontology_distance": distance,
        "foodon_link_method": method,
        "foodon_link_confidence": 1.0,
        "approximate": False,
        "ancestor_level": False,
    }


def test_exact_detail_name_has_priority() -> None:
    decision = SCRIPT.choose_mapping(
        _ingredient("chicken breast fillet"),
        {
            "product_id": "detail__chicken_breast_fillet",
            "method": "exact_detail:ingredient_name",
            "mapping_confidence": "high",
            "automatic": True,
        },
        [_foodon()],
    )
    assert decision.product_id == "detail__chicken_breast_fillet"
    assert decision.review_status == "approved_automatic"


def test_safe_foodon_path_can_replace_alias_evidence() -> None:
    decision = SCRIPT.choose_mapping(
        _ingredient(),
        {
            "product_id": "base__chicken",
            "method": "reviewed_alias:ingredient_name",
            "mapping_confidence": "medium",
            "automatic": True,
        },
        [_foodon()],
    )
    assert decision.product_id == "base__chicken"
    assert decision.method == "foodon_descendant"
    assert decision.ontology_distance == 2
    assert decision.review_status == "approved_automatic"


def test_chicken_stock_is_not_automatically_mapped_to_chicken_meat() -> None:
    decision = SCRIPT.choose_mapping(
        _ingredient("chicken stock"),
        None,
        [_foodon()],
    )
    assert decision.product_id == "base__chicken"
    assert decision.review_status == "needs_review"
    assert decision.reason == "foodon_evidence_not_safe_for_automatic_approval"


def test_approximate_foodon_link_requires_review() -> None:
    candidate = _foodon(method="embedding")
    candidate["foodon_link_confidence"] = 0.91
    decision = SCRIPT.choose_mapping(_ingredient(), None, [candidate])
    assert decision.review_status == "needs_review"


def test_equal_distance_products_are_not_chosen_silently() -> None:
    decision = SCRIPT.choose_mapping(
        _ingredient(),
        None,
        [_foodon("base__chicken"), _foodon("detail__chicken_breast_fillet")],
    )
    assert decision.product_id is None
    assert decision.review_status == "needs_review"
    assert decision.reason == "ambiguous_equal_distance_foodon_candidates"


def test_manual_decision_overrides_automatic_candidates() -> None:
    decision = SCRIPT.choose_mapping(
        _ingredient(),
        None,
        [_foodon()],
        {"decision": "approved", "product_id": "base__chicken"},
    )
    assert decision.product_id == "base__chicken"
    assert decision.review_status == "approved_manual"


def test_anchor_file_allows_multiple_foodon_roots_per_cost_product(
    tmp_path: Path,
) -> None:
    path = tmp_path / "anchors.json"
    path.write_text(
        json.dumps(
            {
                "anchor_version": "test-v2",
                "anchors": [
                    {
                        "product_id": "base__pork",
                        "foodon_id": "FOODON_00001038",
                        "foodon_label": "pork meat food product",
                        "review_status": "approved",
                    },
                    {
                        "product_id": "base__pork",
                        "foodon_id": "FOODON_00004488",
                        "foodon_label": "pork retail cut",
                        "review_status": "approved",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    version, anchors = SCRIPT._anchors(path, {"base__pork"})

    assert version == "test-v2"
    assert [item["foodon_id"] for item in anchors] == [
        "FOODON_00001038",
        "FOODON_00004488",
    ]


def test_anchor_file_rejects_duplicate_product_foodon_pair(tmp_path: Path) -> None:
    path = tmp_path / "anchors.json"
    item = {
        "product_id": "base__pork",
        "foodon_id": "FOODON_00004488",
        "foodon_label": "pork retail cut",
        "review_status": "approved",
    }
    path.write_text(
        json.dumps({"anchor_version": "test-v2", "anchors": [item, item]}),
        encoding="utf-8",
    )

    try:
        SCRIPT._anchors(path, {"base__pork"})
    except ValueError as exc:
        assert "Duplicate approved" in str(exc)
    else:
        raise AssertionError("duplicate anchor pair was accepted")


class _FakeResult:
    def consume(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def run(self, query: str, **parameters: Any) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult()


class _FakeDriver:
    def __init__(self) -> None:
        self.fake_session = _FakeSession()

    def session(self) -> _FakeSession:
        return self.fake_session


def test_apply_query_restores_scope_after_deleting_old_links() -> None:
    driver = _FakeDriver()
    decision = SCRIPT.MappingDecision(
        ingredient_id="ingredient-1",
        ingredient_name="chicken wings",
        product_id="base__chicken",
        method="foodon_descendant",
        mapping_confidence="medium",
        review_status="approved_automatic",
        reason="safe_foodon_descendant_path",
        ingredient_foodon_id="FOODON_00002674",
        cost_anchor_foodon_id="FOODON_00001040",
        ontology_distance=2,
        foodon_link_method="exact_label",
        foodon_link_confidence=1.0,
    )

    SCRIPT._apply_graph(
        driver,
        [{"product_id": "base__chicken", "properties": {}}],
        [],
        [decision],
        "test-anchor-version",
    )

    mapping_query = driver.fake_session.queries[-1]
    assert "FOREACH (" in mapping_query
    assert "CASE WHEN old IS NULL THEN [] ELSE [old] END" in mapping_query
    assert ")\n                WITH ingredient, row\n                MATCH" in mapping_query
