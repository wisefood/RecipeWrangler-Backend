"""Neo4j outage errors must surface as retryable 503s, never generic 500s.

Regression tests for the 2026-07-29 incident: a Neo4j pod reschedule produced
"connection refused" and then TransientError.DatabaseUnavailable, and the
/foodchat_candidates endpoint converted both into 500s, breaking upstream
retry semantics in wisefood-api.
"""

from neo4j.exceptions import (
    DatabaseUnavailable,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from recipe_wrangler.api.error_mapping import map_dependency_error
from recipe_wrangler.api.exceptions import (
    GatewayTimeoutError,
    ServiceUnavailableError,
)


def test_connection_refused_maps_to_503_unavailable():
    exc = Exception(
        "Couldn't connect to neo4j:7687 (resolved to ('10.110.102.0:7687',)): "
        "Failed to establish connection (reason [Errno 111] Connection refused)"
    )
    err = map_dependency_error("Neo4j", exc)
    assert isinstance(err, ServiceUnavailableError)
    assert err.status_code == 503
    assert err.extra["title"] == "DependencyUnavailable"


def test_database_recovering_maps_to_503_unavailable():
    # Message emitted while Neo4j replays its transaction log after a restart.
    exc = Exception(
        "{code: Neo.TransientError.General.DatabaseUnavailable} "
        "{message: The database is not currently available to serve your "
        "request, refer to the database logs for more details. Retrying your "
        "request at a later time may succeed.}"
    )
    err = map_dependency_error("Neo4j", exc)
    assert isinstance(err, ServiceUnavailableError)
    assert err.status_code == 503
    assert err.extra["title"] == "DependencyUnavailable"


def test_database_shutdown_maps_to_503_unavailable():
    exc = Exception("This database is shutdown.")
    err = map_dependency_error("Neo4j", exc)
    assert isinstance(err, ServiceUnavailableError)
    assert err.extra["title"] == "DependencyUnavailable"


def test_timeout_maps_to_504():
    err = map_dependency_error("Neo4j", Exception("connection timed out"))
    assert isinstance(err, GatewayTimeoutError)
    assert err.status_code == 504


def test_unknown_dependency_error_still_maps_to_503():
    err = map_dependency_error("Neo4j", Exception("something exploded"))
    assert isinstance(err, ServiceUnavailableError)
    assert err.status_code == 503
    assert err.extra["title"] == "DependencyError"


def test_driver_exception_hierarchy_matches_endpoint_handler():
    # get_foodchat_candidates catches (Neo4jError, DriverError) to route
    # database failures through map_dependency_error. Every failure mode seen
    # in the incident must fall under one of those two bases.
    assert issubclass(ServiceUnavailable, DriverError)  # connection refused
    assert issubclass(SessionExpired, DriverError)
    assert issubclass(DatabaseUnavailable, TransientError)  # recovery window
    assert issubclass(TransientError, Neo4jError)
