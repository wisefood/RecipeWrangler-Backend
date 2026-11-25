from recipe_wrangler.services.adaptation import adaptation_row as ar


def test_failed_restricted_live_run_is_not_scored_not_stored_row(monkeypatch):
    stored = {"status": "adapted", "reason_code": None, "original": {"lines": ["200g cod"]}, "swap": {}}
    monkeypatch.setattr("recipe_wrangler.repositories.recipe_adaptations.get_adaptation", lambda *a: dict(stored))
    monkeypatch.setitem(ar.MODES, "sustainability", (lambda *a: {"status": "failed", "reason_code": "x"}, True, {}))
    row = ar.adaptation_row("r", "sustainability", "IE", allergens=["fish"])
    assert row["status"] == "not_scored" and not row["is_adapted"]
