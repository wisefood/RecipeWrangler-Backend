"""NEO4J_DATABASE picks the graph every session opens, and only when set."""

import pytest


class _Inner:
    def __init__(self):
        self.calls = []

    def session(self, **kwargs):
        self.calls.append(("session", kwargs))
        return kwargs

    def execute_query(self, *args, **kwargs):
        self.calls.append(("execute_query", kwargs))
        return kwargs

    def close(self):
        return "closed"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("NEO4J_AUTH", "user/pass")
    import recipe_wrangler.utils.neo4j_utils as neo4j_utils
    return neo4j_utils


def test_sessions_open_the_configured_database(module):
    inner = _Inner()
    wrapped = module._DatabaseDriver(inner, "recipewrangler")
    assert wrapped.session(default_access_mode="READ")["database"] == "recipewrangler"
    assert wrapped.execute_query("RETURN 1")["database_"] == "recipewrangler"


def test_an_explicit_database_still_wins(module):
    wrapped = module._DatabaseDriver(_Inner(), "recipewrangler")
    assert wrapped.session(database="system")["database"] == "system"


def test_unset_leaves_sessions_as_they_were(module):
    inner = _Inner()
    wrapped = module._DatabaseDriver(inner, None)
    assert "database" not in wrapped.session()
    assert wrapped.close() == "closed"

