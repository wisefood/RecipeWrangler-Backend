"""GET /api/v1/recipes/{id}/adaptations reads one stored row; the database is mocked."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recipe_wrangler.repositories import recipe_adaptations as repo
from recipe_wrangler.services.adaptation.router import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

ADAPTED = dict(recipe_id="r1", mode="sustainability", region="IE", version="2026-11-18", status="adapted", is_adapted=True, reason_code=None,
               original={"title": "Beef stew"}, adapted={"title": "Chicken stew"}, swap={"candidate_label": "chicken"},
               benefit={"tip": "We replace the beef with chicken because it has 84% lower CO2e per kg."}, provenance={})
NONE = dict(ADAPTED, status="no_suggestion", is_adapted=False, reason_code="no_lower_impact_option", original=None, adapted=None, swap=None, benefit=None)


def fake(row):
    return lambda recipe_id, mode, region: dict(row) if row else None


def test_adapted_row_is_returned(monkeypatch):
    monkeypatch.setattr(repo, "get_adaptation", fake(ADAPTED))
    r = client.get("/api/v1/recipes/r1/adaptations", params={"region": "IE"})
    assert r.status_code == 200 and r.json()["adapted"]["title"] == "Chicken stew" and r.json()["is_adapted"] is True and r.json()["reason"] is None


def test_no_suggestion_row_carries_the_reason_sentence(monkeypatch):
    monkeypatch.setattr(repo, "get_adaptation", fake(NONE))
    body = client.get("/api/v1/recipes/r1/adaptations", params={"region": "IE"}).json()
    assert body["is_adapted"] is False and body["reason_code"] == "no_lower_impact_option" and "lower-impact" in body["reason"]


def test_cost_runs_its_engine_and_a_recipe_without_profile_is_not_scored(monkeypatch):
    from fastapi import HTTPException
    from recipe_wrangler.services.adaptation import service
    monkeypatch.setattr(repo, "get_adaptation", fake(None))
    monkeypatch.setattr(service, "_load_profile", lambda *a: (_ for _ in ()).throw(HTTPException(404)))
    body = client.get("/api/v1/recipes/nope/adaptations", params={"region": "HU", "mode": "cost"}).json()
    assert body["status"] == "not_scored" and body["is_adapted"] is False


def test_missing_row_runs_the_modes_engine(monkeypatch):
    from recipe_wrangler.services.adaptation import adaptation_row as ar
    monkeypatch.setattr(repo, "get_adaptation", fake(None))
    monkeypatch.setitem(ar.MODES, "nutrition", (lambda *a: dict(ADAPTED, mode="nutrition", source="live"), False, {}))
    body = client.get("/api/v1/recipes/r1/adaptations", params={"region": "IE", "mode": "nutrition"}).json()
    assert body["source"] == "live" and body["is_adapted"] is True


def test_bad_region_or_missing_region_is_422():
    assert client.get("/api/v1/recipes/r1/adaptations", params={"region": "XX"}).status_code == 422
    assert client.get("/api/v1/recipes/r1/adaptations").status_code == 422
