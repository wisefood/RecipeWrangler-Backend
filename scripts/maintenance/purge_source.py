#!/usr/bin/env python3
"""Hard-delete every trace of a recipe source from Neo4j, Elasticsearch and Postgres.

This is the destructive counterpart to ``scripts/disable_recipes.py``. Where that
script flips ``status`` and leaves the data in place, this one removes it. There
is no undo — take a dump first (see ``--require-dump``).

Nutrition profiles are handled separately from recipes, because a source's
*original* nutrition data can be worth keeping as a reference dataset even once
its recipes are gone. ``--keep-nutrition-source`` names the profile rows to
spare; everything else belonging to the source is deleted.

Usage
-----
  # Dry-run (default): report exactly what would be deleted, per store
  python scripts/maintenance/purge_source.py --source recipe1m

  # Purge recipe1m, but keep its original nutrition rows as reference data
  python scripts/maintenance/purge_source.py --source recipe1m \
      --keep-nutrition-source recipe1m_original --apply

  # Limit to one store (repeat or comma-separate)
  python scripts/maintenance/purge_source.py --source recipe1m \
      --stores neo4j --apply

Notes
-----
Neo4j deletes run through ``apoc.periodic.iterate`` in independent
transactions, so transaction logs rotate instead of growing to the size of the
whole operation. On a volume that is already near full, keep ``--batch-size``
modest and watch ``df -h /data`` in the pod while it runs.

Deleting rows does not shrink the Neo4j store or the Postgres table on disk;
the space is reused internally. Run a dump/reload (Neo4j) or ``VACUUM FULL``
(Postgres) afterwards to return it to the filesystem.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

import os

from sqlalchemy import bindparam, text

from recipe_wrangler.utils.neo4j_utils import driver, run_query
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

logger = logging.getLogger("purge_source")

ALL_STORES = ("neo4j", "es", "postgres")


def _profiles_table() -> str:
    return _get_config()["profiles_table"]


def _keep_clause(keep: list[str], *, negated: bool) -> tuple[str, list]:
    """Build the nutrition_source predicate and the bindparams it needs.

    ``negated=True`` selects the rows to delete (everything not spared);
    ``negated=False`` selects the spared rows. With an empty ``keep`` list the
    predicate collapses away rather than emitting an invalid ``IN ()``.
    """
    if not keep:
        return ("" if negated else " AND false"), []
    op = "NOT IN" if negated else "IN"
    return f" AND nutrition_source {op} :keep", [bindparam("keep", expanding=True)]


# --------------------------------------------------------------------------- #
# counts
# --------------------------------------------------------------------------- #
def neo4j_count(source: str) -> int:
    rows = run_query(
        "MATCH (r:Recipe {source: $source}) RETURN count(r) AS c",
        {"source": source},
    )
    return int(rows[0]["c"]) if rows else 0


def es_count(es_url: str, index: str, source: str) -> int:
    resp = requests.post(
        f"{es_url.rstrip('/')}/{index}/_count",
        json={"query": {"term": {"source": source}}},
        timeout=60,
    )
    resp.raise_for_status()
    return int(resp.json().get("count", 0))


def postgres_counts(source: str, keep: list[str]) -> tuple[int, int]:
    """Return (rows_to_delete, rows_kept_for_this_source)."""
    table = _profiles_table()
    del_pred, del_binds = _keep_clause(keep, negated=True)
    keep_pred, keep_binds = _keep_clause(keep, negated=False)

    del_stmt = text(
        f'SELECT count(*) FROM "{table}" WHERE source = :source{del_pred}'
    )
    keep_stmt = text(
        f'SELECT count(*) FROM "{table}" WHERE source = :source{keep_pred}'
    )
    if del_binds:
        del_stmt = del_stmt.bindparams(*del_binds)
    if keep_binds:
        keep_stmt = keep_stmt.bindparams(*keep_binds)

    params = {"source": source}
    if keep:
        params["keep"] = keep

    with get_connection() as conn:
        to_delete = int(conn.execute(del_stmt, params).scalar_one())
        kept = int(conn.execute(keep_stmt, params).scalar_one())
    return to_delete, kept


# --------------------------------------------------------------------------- #
# deletes
# --------------------------------------------------------------------------- #
def purge_neo4j(source: str, batch_size: int) -> int:
    """DETACH DELETE the source's recipes in independent batches.

    ``apoc.periodic.iterate`` opens and commits its own transactions, so it must
    be issued as an auto-commit query — wrapping it in a managed transaction
    (``session.execute_write``) makes it fail as a nested transaction. That is
    why this bypasses ``run_query``.
    """
    query = """
        CALL apoc.periodic.iterate(
          'MATCH (r:Recipe {source: $source}) RETURN r',
          'DETACH DELETE r',
          {batchSize: $batchSize, parallel: false, params: {source: $source}}
        )
        YIELD batches, total, failedBatches, errorMessages
        RETURN batches, total, failedBatches, errorMessages
    """
    with driver.session() as session:
        rows = list(session.run(query, {"source": source, "batchSize": batch_size}))
    if not rows:
        return 0
    rec = rows[0]
    failed = rec.get("failedBatches") or 0
    if failed:
        logger.error(
            "neo4j: %s batch(es) failed: %s", failed, rec.get("errorMessages")
        )
    logger.info(
        "neo4j: deleted %s recipes in %s batches", rec.get("total"), rec.get("batches")
    )
    return int(rec.get("total") or 0)


def purge_es(es_url: str, index: str, source: str, poll_seconds: float) -> int:
    """Delete by query asynchronously, polling the task until it completes."""
    resp = requests.post(
        f"{es_url.rstrip('/')}/{index}/_delete_by_query",
        params={
            "conflicts": "proceed",
            "wait_for_completion": "false",
            "slices": "auto",
        },
        json={"query": {"term": {"source": source}}},
        timeout=60,
    )
    resp.raise_for_status()
    task_id = resp.json().get("task")
    if not task_id:
        raise RuntimeError(f"no task id returned: {resp.text[:200]}")
    logger.info("es: delete_by_query task %s started", task_id)

    while True:
        time.sleep(poll_seconds)
        status = requests.get(
            f"{es_url.rstrip('/')}/_tasks/{task_id}", timeout=60
        )
        status.raise_for_status()
        payload = status.json()
        if payload.get("completed"):
            response = payload.get("response", {})
            failures = response.get("failures") or []
            if failures:
                logger.error("es: %s failure(s): %s", len(failures), failures[:3])
            deleted = int(response.get("deleted", 0))
            logger.info("es: deleted %s documents", deleted)
            return deleted
        st = payload.get("task", {}).get("status", {})
        logger.info(
            "es: %s/%s deleted...", st.get("deleted", 0), st.get("total", "?")
        )


def purge_postgres(source: str, keep: list[str], batch_size: int) -> int:
    """Delete the source's profile rows in batches, sparing ``keep``."""
    table = _profiles_table()
    pred, binds = _keep_clause(keep, negated=True)
    stmt = text(
        f'DELETE FROM "{table}" WHERE ctid IN ('
        f'  SELECT ctid FROM "{table}" '
        f"  WHERE source = :source{pred} LIMIT :batch)"
    )
    if binds:
        stmt = stmt.bindparams(*binds)

    params = {"source": source, "batch": batch_size}
    if keep:
        params["keep"] = keep

    deleted = 0
    with get_connection() as conn:
        while True:
            n = conn.execute(stmt, params).rowcount
            conn.commit()
            if n <= 0:
                break
            deleted += n
            logger.info("postgres: deleted %s rows (running total)", deleted)
    return deleted


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Hard-delete a recipe source from every store."
    )
    ap.add_argument("--source", required=True, help="Recipe source to purge, e.g. recipe1m")
    ap.add_argument(
        "--keep-nutrition-source",
        action="append",
        default=[],
        metavar="NAME",
        help="Profile rows with this nutrition_source survive (repeatable).",
    )
    ap.add_argument(
        "--stores",
        default=",".join(ALL_STORES),
        help=f"Comma-separated subset of {ALL_STORES}.",
    )
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument(
        "--es-url", default=os.getenv("ELASTIC_URL", "http://localhost:9200")
    )
    ap.add_argument("--index", default=os.getenv("ELASTIC_INDEX", "recipes_v2"))
    ap.add_argument(
        "--require-dump",
        metavar="PATH",
        help="Refuse to run unless this file exists — point it at your safety dump.",
    )
    ap.add_argument("--apply", action="store_true", help="Actually delete.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    stores = [s.strip() for s in args.stores.split(",") if s.strip()]
    unknown = set(stores) - set(ALL_STORES)
    if unknown:
        ap.error(f"unknown store(s): {sorted(unknown)}")

    if args.require_dump and not Path(args.require_dump).exists():
        ap.error(f"--require-dump given but missing: {args.require_dump}")

    keep = sorted(set(args.keep_nutrition_source))

    logger.info("source=%s stores=%s keep_nutrition_source=%s", args.source, stores, keep)

    # ---- report ---------------------------------------------------------- #
    if "neo4j" in stores:
        logger.info("neo4j: %s recipes match", neo4j_count(args.source))
    if "es" in stores:
        logger.info(
            "es: %s documents match in %s",
            es_count(args.es_url, args.index, args.source),
            args.index,
        )
    if "postgres" in stores:
        to_delete, kept = postgres_counts(args.source, keep)
        logger.info("postgres: %s rows to delete, %s rows spared", to_delete, kept)

    if not args.apply:
        logger.info("dry-run — nothing deleted. Re-run with --apply to execute.")
        return

    # ---- execute --------------------------------------------------------- #
    if "neo4j" in stores:
        purge_neo4j(args.source, args.batch_size)
    if "es" in stores:
        purge_es(args.es_url, args.index, args.source, args.poll_seconds)
    if "postgres" in stores:
        purge_postgres(args.source, keep, args.batch_size)

    logger.info("purge complete for source=%s", args.source)


if __name__ == "__main__":
    main()
