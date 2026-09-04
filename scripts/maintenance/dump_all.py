#!/usr/bin/env python3
"""Dump all three stores to a timestamped directory.

Produces the baseline a fresh environment is seeded from, and the rollback
point before a risky change. All three are written together on purpose: a
recipe's content lives in Neo4j, its nutrition in Postgres and its annotations
only in Elasticsearch, so a dump of any one of them alone is not a restorable
state.

Elasticsearch is dumped as newline-delimited JSON with the mapping alongside,
rather than as a snapshot: snapshots need a registered repository on the
cluster, NDJSON needs nothing and can be inspected, diffed and partially
restored.

Usage
-----
  python scripts/maintenance/dump_all.py                       # all three
  python scripts/maintenance/dump_all.py --stores elastic      # just one
  python scripts/maintenance/dump_all.py --out /path/to/dir
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

import os

from recipe_wrangler.api.config import get_settings

logger = logging.getLogger("dump_all")

ES_INDICES = ("recipes", "ingredient_vectors")
PG_TABLES = (
    "nutrients-recipe-profiles",
    "nutrients-ingredients-eu",
    "nutrients-ingredients-irish",
    "nutrients-ingredients-hungarian",
    "pipeline_static_data",
)


def dump_elastic(out: Path) -> dict[str, int]:
    """Stream every document of each index to NDJSON, plus its mapping."""
    from recipe_wrangler.catalog.elastic import get_catalog_client

    client = get_catalog_client()
    counts: dict[str, int] = {}
    for alias in ES_INDICES:
        if not (client.alias_exists(alias) or client.index_exists(alias)):
            logger.warning("elastic: %s does not exist, skipping", alias)
            continue

        mapping = client._request("GET", f"{alias}/_mapping")
        (out / f"elastic-{alias}.mapping.json").write_text(
            json.dumps(mapping, indent=2, sort_keys=True)
        )

        path = out / f"elastic-{alias}.ndjson.gz"
        written = 0
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for hit in client.scroll_all(alias, page_size=500):
                handle.write(
                    json.dumps({"_id": hit["_id"], "_source": hit["_source"]}) + "\n"
                )
                written += 1
        counts[alias] = written
        logger.info("elastic: %s -> %s docs (%s)", alias, written, path.name)
    return counts


def dump_postgres(out: Path) -> dict[str, int]:
    """COPY each table to gzipped CSV with a header."""
    from sqlalchemy import text

    from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection

    cfg = _get_config()
    counts: dict[str, int] = {}

    # The schema first: the CSVs are data only, and restoring them needs the
    # DDL. pg_dump is used only for this, so the dump does not depend on it
    # being available for the bulk data.
    schema_path = out / "postgres-schema.sql"
    try:
        env = os.environ.copy()
        env["PGPASSWORD"] = str(cfg["db_password"])
        subprocess.run(
            ["pg_dump", "--schema-only", "--no-owner", "--no-privileges",
             "-h", str(cfg["db_host"]), "-p", str(cfg["db_port"]),
             "-U", str(cfg["db_user"]), "-d", str(cfg["db_name"]),
             "-f", str(schema_path)],
            check=True, env=env, capture_output=True, timeout=300,
        )
        logger.info("postgres: schema -> %s", schema_path.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("postgres: pg_dump unavailable (%s); data still dumped", exc)

    with get_connection() as conn:
        raw = conn.connection.driver_connection
        for table in PG_TABLES:
            try:
                total = conn.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar_one()
            except Exception:
                logger.warning("postgres: %s not present, skipping", table)
                continue
            path = out / f"postgres-{table}.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle, raw.cursor() as cur:
                cur.copy_expert(
                    f'COPY "{table}" TO STDOUT WITH (FORMAT csv, HEADER)', handle
                )
            counts[table] = int(total)
            logger.info("postgres: %s -> %s rows (%s)", table, total, path.name)
    return counts


NEO4J_EXPORT = """
MATCH (n)
RETURN labels(n) AS labels, properties(n) AS props, elementId(n) AS eid
"""

NEO4J_RELS = """
MATCH (a)-[r]->(b)
RETURN type(r) AS type, properties(r) AS props,
       elementId(a) AS src, elementId(b) AS dst
"""


def dump_neo4j(out: Path) -> dict[str, int]:
    """Stream nodes and relationships to NDJSON.

    Not ``neo4j-admin dump``: that needs the database stopped (or an online
    backup port) and writes into the pod's own volume, which on this deployment
    is 93% full. Streaming out over Bolt needs neither.
    """
    from recipe_wrangler.utils.neo4j_utils import driver

    counts: dict[str, int] = {}
    nodes_path = out / "neo4j-nodes.ndjson.gz"
    with gzip.open(nodes_path, "wt", encoding="utf-8") as handle:
        written = 0
        with driver.session() as session:
            for record in session.run(NEO4J_EXPORT):
                handle.write(
                    json.dumps(
                        {
                            "id": record["eid"],
                            "labels": record["labels"],
                            "properties": record["props"],
                        },
                        default=str,
                    )
                    + "\n"
                )
                written += 1
        counts["nodes"] = written
    logger.info("neo4j: %s nodes (%s)", counts["nodes"], nodes_path.name)

    rels_path = out / "neo4j-relationships.ndjson.gz"
    with gzip.open(rels_path, "wt", encoding="utf-8") as handle:
        written = 0
        with driver.session() as session:
            for record in session.run(NEO4J_RELS):
                handle.write(
                    json.dumps(
                        {
                            "type": record["type"],
                            "start": record["src"],
                            "end": record["dst"],
                            "properties": record["props"],
                        },
                        default=str,
                    )
                    + "\n"
                )
                written += 1
        counts["relationships"] = written
    logger.info("neo4j: %s relationships (%s)", counts["relationships"], rels_path.name)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stores", default="elastic,postgres,neo4j")
    ap.add_argument("--out", default=None, help="Target directory (default: dumps/<timestamp>)")
    ap.add_argument("--label", default="", help="Suffix for the directory name.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else REPO_ROOT / "dumps" / "local" / (
        f"{stamp}{('-' + args.label) if args.label else ''}"
    )
    out.mkdir(parents=True, exist_ok=True)
    logger.info("dumping to %s", out)

    stores = {s.strip() for s in args.stores.split(",") if s.strip()}
    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elastic_index_alias": get_settings().elastic_index,
        "stores": {},
    }

    if "elastic" in stores:
        manifest["stores"]["elastic"] = dump_elastic(out)
    if "postgres" in stores:
        manifest["stores"]["postgres"] = dump_postgres(out)
    if "neo4j" in stores:
        manifest["stores"]["neo4j"] = dump_neo4j(out)

    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    total = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    logger.info("done: %s files, %.1f MB", len(list(out.iterdir())), total / 1e6)
    logger.info("manifest: %s", json.dumps(manifest["stores"]))


if __name__ == "__main__":
    main()
