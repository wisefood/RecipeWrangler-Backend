import os
import re

from neo4j import READ_ACCESS, WRITE_ACCESS, GraphDatabase

# Set up the connection
uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")

# Support either NEO4J_AUTH="username/password" or NEO4J_USERNAME + NEO4J_PASSWORD.
neo4j_auth = os.getenv("NEO4J_AUTH")
if neo4j_auth:
    username, password = neo4j_auth.split("/", 1)
else:
    username = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not username or not password:
        raise ValueError(
            "Set NEO4J_AUTH (username/password) or NEO4J_USERNAME + NEO4J_PASSWORD."
        )

# The driver is created once per process and shared: it owns the connection
# pool, and each session() call borrows a pooled connection rather than
# dialing a new one. Pool knobs are surfaced as env vars with driver defaults.
driver = GraphDatabase.driver(
    uri,
    auth=(username, password),
    max_transaction_retry_time=float(os.getenv("NEO4J_MAX_RETRY_TIME_SECONDS", "30")),
    max_connection_pool_size=int(os.getenv("NEO4J_MAX_POOL_SIZE", "100")),
    connection_acquisition_timeout=float(
        os.getenv("NEO4J_POOL_ACQUISITION_TIMEOUT_SECONDS", "60")
    ),
    max_connection_lifetime=float(os.getenv("NEO4J_MAX_CONNECTION_LIFETIME_SECONDS", "3600")),
)


# Cypher clauses that mutate. Matched as whole words so a property named
# `created_at` or a label `Merged` does not make a read look like a write.
_WRITE_CLAUSES = (
    "create", "merge", "set", "delete", "remove", "detach",
    "foreach", "drop", "load csv",
)
_WRITE_PATTERN = re.compile(
    r"\b(" + "|".join(c.replace(" ", r"\s+") for c in _WRITE_CLAUSES) + r")\b",
    re.IGNORECASE,
)


def _is_write(query: str) -> bool:
    """Whether a statement mutates, ignoring comments and string literals.

    Both are stripped first because a comment explaining a MERGE, or a string
    parameter containing the word "set", would otherwise classify a read as a
    write — harmless, but it puts the query back on the leader for no reason.

    Deliberately biased toward "write" when unsure. Misjudging a write as a read
    fails loudly: Neo4j refuses mutations inside a read transaction. Misjudging
    a read as a write is merely the behaviour this function replaced.
    """
    text = re.sub(r"//[^\n]*", " ", query or "")
    text = re.sub(r"'[^']*'|\"[^\"]*\"", " ", text)
    return bool(_WRITE_PATTERN.search(text)) or "apoc.periodic" in text.lower()


# Function to run a Cypher query
def run_query(query, parameters=None):
    """Run Cypher in a managed transaction, routed by what it actually does.

    Managed so the driver retries transient failures (TransientError,
    ServiceUnavailable, SessionExpired) with backoff up to
    max_transaction_retry_time — the recovery window after a Neo4j restart.

    Reads go through `execute_read`, writes through `execute_write`. Sending
    everything through `execute_write` had two costs: a non-idempotent statement
    could be applied twice when the driver retried a transient disconnect (the
    write may have committed before the connection dropped), and every read was
    routed to the leader, so read replicas took no load at all.
    """
    write = _is_write(query)
    with driver.session(
        default_access_mode=WRITE_ACCESS if write else READ_ACCESS
    ) as session:
        runner = session.execute_write if write else session.execute_read
        return runner(lambda tx: list(tx.run(query, parameters)))
