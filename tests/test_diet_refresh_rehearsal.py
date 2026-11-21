"""Stored refresh must stay scoped, refuse stale inputs and verify every rollback."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from scripts.neo4j import rehearse_diet_refresh as service
from recipe_wrangler.utils import dietary_classification as dc
from recipe_wrangler.utils.consumer_suitability import classification_version


def snapshot(*, refreshed=False):
    groups = ["vegan", "vegetarian"]
    nodes = [{"id": "i", "properties": {"name": "rice", "source_claim": "preserve"}}]
    nodes += [{"id": rid, "properties": {"recipe_id": rid, "tags": ["source-vegan"]}} for rid in ["r1", "r2"]]
    links = [{"id": "membership", "type": "HAS_INGREDIENT", "start": "r2", "end": "i", "properties": {}}]
    for node in nodes:
        for group in groups:
            links.append(dict(id=node["id"]+group, start=node["id"], end=group, type="SUITABILITY_FOR",
                target_properties={"name":group}, properties=dict(status="suitable" if refreshed else "unknown",
                    scope="ingredient_composition" if node["id"]=="i" else "recipe_composition",
                    classification_version=classification_version(group) if refreshed else "vegan-vegetarian-v1",
                    sources=["rules"])))
    return dict(seed_recipe_ids=["r1"], ingredient_ids=["i"], affected_recipe_ids=["r1", "r2"],
                recipe_node_ids=["r1", "r2"], groups=groups, nodes=nodes, relationships=links)


@pytest.fixture
def prepared(monkeypatch):
    before, after = snapshot(), snapshot(refreshed=True)
    monkeypatch.setattr(service, "require_bounded_job", lambda: None)
    monkeypatch.setattr(service, "code_hashes", lambda: {"code":"frozen"})
    captures = iter([deepcopy(before), deepcopy(after), deepcopy(before)])
    monkeypatch.setattr(service, "capture", lambda *args: next(captures))
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    tx = session.begin_transaction.return_value
    calls=[]
    for name in ["_initialize_unknown", "_classify_negative", "_classify_positive"]:
        def execute(transaction, group, ids, name=name):
            assert transaction is tx
            calls.append((name, group, ids))
            return len(ids)
        monkeypatch.setattr(dc, name, execute)
    def aggregate(transaction, ids, group):
        assert transaction is tx
        calls.append(("aggregate", group, ids))
        return len(ids)
    monkeypatch.setattr(dc, "_aggregate_recipe_batch", aggregate)
    plan=dict(kind="stored_diet_rollback_only_v1", code_sha256=service.code_hashes(),
              before=before, before_sha256=service.digest(before), allergen_evidence="existing_stored_evidence_only")
    return driver, tx, plan, before, after, calls


def test_rehearsal_uses_one_explicit_transaction_and_refreshes_shared_recipe(prepared):
    driver, tx, plan, before, after, calls = prepared
    result = service.rehearse(driver, plan)
    assert result["status"]=="passed" and result["rollback_verified"]
    assert result["persisted_writes"] is False
    assert result["affected_recipe_ids"]==["r1", "r2"]
    assert calls[:4]==[("_initialize_unknown", "vegan", ["i"]), ("_classify_negative", "vegan", ["i"]),
                      ("_classify_positive", "vegan", ["i"]), ("aggregate", "vegan", ["r1", "r2"])]
    driver.session.return_value.__enter__.return_value.begin_transaction.assert_called_once_with(timeout=60)
    assert driver.session.call_args.kwargs=={"default_access_mode":"READ", "fetch_size":100}
    tx.rollback.assert_called_once()
    tx.close.assert_called_once()
    tx.commit.assert_not_called()
    driver.session.return_value.__enter__.return_value.execute_write.assert_not_called()


@pytest.mark.parametrize("failure", ["injected", "classification", "protected", "restore", "stale"])
def test_failures_always_roll_back_and_do_not_claim_success(prepared, monkeypatch, failure):
    driver, tx, plan, before, after, calls = prepared
    if failure=="classification":
        monkeypatch.setattr(dc, "_classify_negative", MagicMock(side_effect=RuntimeError("failed")))
    if failure=="protected":
        after["nodes"][0]["properties"]["source_claim"]="lost"
    restored=deepcopy(before)
    if failure=="restore":
        restored["nodes"][0]["properties"]["source_claim"]="concurrent change"
    current=deepcopy(before)
    if failure=="stale":
        current["affected_recipe_ids"].append("new sibling")
    captures=[current, restored] if failure in {"injected", "classification", "stale"} else [current, after, restored]
    monkeypatch.setattr(service, "capture", MagicMock(side_effect=captures))
    result=service.rehearse(driver, plan, inject_failure=failure=="injected")
    assert result["status"]=="failed"
    assert result["rollback_verified"]==(failure!="restore")
    assert "error" in result
    assert result["persisted_writes"] is (None if failure=="restore" else False)
    tx.rollback.assert_called_once()
    tx.commit.assert_not_called()
    if failure=="stale":
        assert not calls


@pytest.mark.parametrize("change", ["code", "plan", "kind"])
def test_changed_code_or_plan_stops_before_any_transaction(prepared, change):
    driver, tx, plan, *_ = prepared
    if change=="code": plan["code_sha256"]={"code":"changed"}
    elif change=="plan": plan["before"]["ingredient_ids"].append("foreign")
    else: plan["kind"]="apply"
    with pytest.raises(ValueError):
        service.rehearse(driver, plan)
    driver.session.assert_not_called()


@pytest.mark.parametrize("change", ["source", "version", "scope", "duplicate", "newer"])
def test_foreign_or_ambiguous_assessment_is_never_overwritten(change):
    before=snapshot()
    assessment=service.assessments(before)[0]
    if change=="source": assessment["properties"]["sources"]=["manual"]
    elif change=="version": assessment["properties"]["classification_version"]="foreign-v1"
    elif change=="scope": assessment["properties"]["scope"]="certification"
    elif change=="newer": assessment["properties"]["classification_version"]="vegan-vegetarian-v99"
    else: before["relationships"].append(deepcopy(assessment))
    with pytest.raises(ValueError):
        service.check_ownership(before)


def test_overflow_does_not_silently_truncate_scope(monkeypatch):
    monkeypatch.setattr(service, "rows", lambda *args, **kwargs: [{"id":i} for i in range(100)])
    with pytest.raises(ValueError, match="Scope exceeds"):
        service.bounded(object(), "MATCH (n) RETURN n")


def test_snapshot_budget_and_cursor_are_enforced(monkeypatch):
    monkeypatch.setattr(service, "MAX_SNAPSHOT_ROWS", 1)
    monkeypatch.setattr(service, "rows", lambda *args, **kwargs: [{"id":"a"}, {"id":"b"}])
    with pytest.raises(ValueError, match="row budget"):
        service.paged(object(), "query")
    monkeypatch.setattr(service, "MAX_SNAPSHOT_ROWS", 10)
    monkeypatch.setattr(service, "rows", lambda *args, **kwargs: [{"id":"a"}])
    with pytest.raises(ValueError, match="did not advance"):
        service.paged(object(), "query")


def test_unbounded_job_stops_before_any_transaction(prepared, monkeypatch):
    driver, *_ = prepared
    monkeypatch.setattr(service, "require_bounded_job", MagicMock(side_effect=ValueError("Unbounded")))
    with pytest.raises(ValueError, match="Unbounded"):
        service.rehearse(driver, prepared[2])
    driver.session.assert_not_called()


def test_unavailable_post_rollback_read_does_not_claim_verified_restore(prepared, monkeypatch):
    driver, tx, plan, before, after, calls = prepared
    monkeypatch.setattr(service, "capture", MagicMock(side_effect=[before, after, RuntimeError("offline")]))
    result=service.rehearse(driver, plan)
    assert result["status"]=="failed" and not result["rollback_verified"]
    assert result["persisted_writes"] is None
    tx.rollback.assert_called_once()


def test_retained_ingredient_evidence_is_protected_even_for_the_selected_groups():
    before=snapshot()
    dependency=deepcopy(service.assessments(before)[0])
    dependency.update(id="retained-vegan", start="untouched-ingredient")
    before["relationships"].append(dependency)
    after=deepcopy(before)
    after["relationships"][-1]["properties"]["status"]="suitable"
    assert service.protected(before)!=service.protected(after)
    assert dependency not in service.assessments(before)


def test_rollback_error_cannot_leave_success_status_even_if_close_restores_state(prepared):
    driver, tx, plan, *_ = prepared
    tx.rollback.side_effect=RuntimeError("rollback acknowledgement failed")
    result=service.rehearse(driver, plan)
    assert result["status"]=="failed" and result["error"]=="RuntimeError"
    assert result["rollback_verified"]
    tx.close.assert_called_once()
