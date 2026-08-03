#!/usr/bin/env python3
"""Restore a dumps/<stamp>/ bundle into Neo4j, Postgres and Elasticsearch.

Counterpart of dump_all_lite.py. Defaults target the repo's LOCAL docker
compose stacks (neo4j-docker/, postgresql-docker/, elasticsearch-docker/), on
purpose: a restore should never point at production unless every endpoint is
given explicitly.

Native dumps are restored with the native mechanisms:
- nutrients.dump  -> pg_restore --clean (dockerized, version-matched client)
- neo4j.dump      -> neo4j-admin database load, by stopping the target
                     container, loading into its /data volume, restarting it

Older bundles without native dumps fall back to postgres-*.csv.gz (+ schema)
and neo4j-*.ndjson.gz (which needs APOC on the target and wipes the graph
over Bolt).

Usage:
  python3 scripts/maintenance/restore_all_lite.py dumps/<stamp> \
      [--stores elastic,postgres,neo4j] \
      [--es-url http://localhost:9200] \
      [--neo4j-container neo4j-apoc] \
      [--neo4j-uri bolt://localhost:7687] [--neo4j-auth neo4j/password123] \
      [--pg-host localhost] [--pg-port 5435] [--pg-db nutrients] \
      [--pg-user postgres] [--pg-password postgres]

Targets are wiped before loading. Never point this at production.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger("restore_all_lite")

BATCH = 1000

# Index-scoped settings Elasticsearch refuses at creation time.
NON_RESTORABLE_SETTINGS = {
    "creation_date", "uuid", "version", "provided_name", "routing", "history",
}


def _docker(*args: str, env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], env=env, capture_output=True, check=True, text=True, **kwargs
    )


# ── Elasticsearch ────────────────────────────────────────────────────────────

def _es(base: str, path: str, body=None, method: str = "GET", ndjson: str | None = None):
    if ndjson is not None:
        data = ndjson.encode()
        ctype = "application/x-ndjson"
    else:
        data = json.dumps(body).encode() if body is not None else None
        ctype = "application/json"
    req = Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", ctype)
    with urlopen(req, timeout=300) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def restore_elastic(bundle: Path, base: str) -> None:
    base = base.rstrip("/")
    for mapping_file in sorted(bundle.glob("elastic-*.mapping.json")):
        alias = mapping_file.name[len("elastic-"):-len(".mapping.json")]
        ndjson_file = bundle / f"elastic-{alias}.ndjson.gz"
        if not ndjson_file.exists():
            logger.warning("elastic: no data file for %s, skipping", alias)
            continue

        mapping_payload = json.loads(mapping_file.read_text())
        # The export is keyed by the concrete index name the alias pointed at.
        source_index, index_payload = next(iter(mapping_payload.items()))
        mappings = index_payload["mappings"]

        body: dict = {"mappings": mappings}
        settings_file = bundle / f"elastic-{alias}.settings.json"
        if settings_file.exists():
            settings_payload = json.loads(settings_file.read_text())
            index_settings = next(iter(settings_payload.values()))["settings"]["index"]
            cleaned = {
                k: v for k, v in index_settings.items()
                if k not in NON_RESTORABLE_SETTINGS
            }
            if cleaned:
                body["settings"] = {"index": cleaned}

        try:
            _es(base, f"/{source_index}", method="DELETE")
        except HTTPError as exc:
            if exc.code != 404:
                raise
        _es(base, f"/{source_index}", body=body, method="PUT")
        logger.info("elastic: created index %s", source_index)

        lines: list[str] = []
        indexed = 0

        def flush(lines: list[str]) -> int:
            result = _es(base, "/_bulk", ndjson="\n".join(lines) + "\n", method="POST")
            if result.get("errors"):
                bad = [i for i in result["items"] if i["index"].get("error")][:3]
                raise RuntimeError(f"bulk errors for {source_index}: {bad}")
            return len(lines) // 2

        with gzip.open(ndjson_file, "rt", encoding="utf-8") as handle:
            for raw in handle:
                record = json.loads(raw)
                lines.append(json.dumps({"index": {"_index": source_index, "_id": record["_id"]}}))
                lines.append(json.dumps(record["_source"], ensure_ascii=False))
                if len(lines) >= BATCH * 2:
                    indexed += flush(lines)
                    lines = []
        if lines:
            indexed += flush(lines)
        _es(base, f"/{source_index}/_refresh", method="POST")

        if alias != source_index:
            _es(base, "/_aliases", body={
                "actions": [{"add": {"index": source_index, "alias": alias}}]
            }, method="POST")
        logger.info("elastic: %s -> %s docs (alias %s)", source_index, indexed, alias)


# ── Postgres ─────────────────────────────────────────────────────────────────

def _pg_server_major(cfg: dict) -> str:
    import psycopg2

    admin = dict(cfg, dbname="postgres")
    conn = psycopg2.connect(**admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        version = cur.fetchone()[0]
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg["dbname"],))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{cfg["dbname"]}"')
            logger.info("postgres: created database %s", cfg["dbname"])
    conn.close()
    return version.split(".")[0]


def restore_postgres(bundle: Path, cfg: dict) -> None:
    native = bundle / "nutrients.dump"
    if native.exists():
        # The client must be at least as new as the pg_dump that wrote the
        # archive (prod is on 17), regardless of the target server's version.
        major = max(int(_pg_server_major(cfg)), 17)
        image = f"postgres:{major}-alpine"
        logger.info("postgres: pg_restore %s via %s", native.name, image)
        env = os.environ.copy()
        env["PGPASSWORD"] = str(cfg["password"])
        host = "127.0.0.1" if cfg["host"] in ("localhost", "127.0.0.1") else cfg["host"]
        subprocess.run(
            [
                "docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
                "-v", f"{bundle.resolve()}:/dumps:ro",
                image,
                "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
                "-h", host, "-p", str(cfg["port"]),
                "-U", str(cfg["user"]), "-d", str(cfg["dbname"]),
                "/dumps/nutrients.dump",
            ],
            env=env, capture_output=True, check=True, timeout=3600,
        )
        _pg_log_counts(cfg)
        return

    logger.info("postgres: no nutrients.dump, falling back to schema + CSVs")
    _restore_postgres_csvs(bundle, cfg)


def _pg_log_counts(cfg: dict) -> None:
    import psycopg2

    conn = psycopg2.connect(**cfg)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
        )
        for (table,) in cur.fetchall():
            with conn.cursor() as inner:
                inner.execute(f'SELECT count(*) FROM "{table}"')
                logger.info("postgres: %s -> %s rows", table, inner.fetchone()[0])
    conn.close()


def _restore_postgres_csvs(bundle: Path, cfg: dict) -> None:
    import psycopg2

    conn = psycopg2.connect(**cfg)
    conn.autocommit = True

    schema_file = bundle / "postgres-schema.sql"
    if schema_file.exists():
        with conn.cursor() as cur:
            cur.execute(schema_file.read_text())
        logger.info("postgres: applied %s", schema_file.name)
    else:
        logger.warning("postgres: no postgres-schema.sql; tables must already exist")

    for path in sorted(bundle.glob("postgres-*.csv.gz")):
        table = path.name[len("postgres-"):-len(".csv.gz")]
        with conn.cursor() as cur:
            cur.execute(f'TRUNCATE "{table}"')
        with gzip.open(path, "rt", encoding="utf-8") as handle, conn.cursor() as cur:
            cur.copy_expert(f'COPY "{table}" FROM STDIN WITH (FORMAT csv, HEADER)', handle)
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{table}"')
            logger.info("postgres: %s -> %s rows", table, cur.fetchone()[0])
    conn.close()


# ── Neo4j ────────────────────────────────────────────────────────────────────

def restore_neo4j_native(bundle: Path, container: str) -> None:
    """Stop the target container, neo4j-admin database load, restart."""
    inspect = json.loads(_docker("inspect", container).stdout)[0]
    image = inspect["Config"]["Image"]
    data_volume = next(
        (m["Name"] for m in inspect.get("Mounts", []) if m.get("Destination") == "/data"),
        None,
    )
    if not data_volume:
        raise RuntimeError(f"no /data volume on container {container}")

    was_running = inspect["State"]["Running"]
    if was_running:
        logger.info("neo4j: stopping %s", container)
        _docker("stop", container)
    try:
        logger.info("neo4j: neo4j-admin database load from %s", bundle / "neo4j.dump")
        _docker(
            "run", "--rm",
            "-e", "NEO4J_ACCEPT_LICENSE_AGREEMENT=yes",
            "-v", f"{data_volume}:/data",
            "-v", f"{bundle.resolve()}:/dumps:ro",
            image,
            "neo4j-admin", "database", "load", "neo4j",
            "--from-path=/dumps", "--overwrite-destination=true",
        )
    finally:
        if was_running:
            _docker("start", container)
            logger.info("neo4j: restarted %s", container)
    logger.info("neo4j: native load complete")


def restore_neo4j_ndjson(bundle: Path, uri: str, auth: tuple[str, str]) -> None:
    """Fallback for old bundles: wipe the graph and reload over Bolt (needs APOC)."""
    from neo4j import GraphDatabase

    nodes_file = bundle / "neo4j-nodes.ndjson.gz"
    rels_file = bundle / "neo4j-relationships.ndjson.gz"
    driver = GraphDatabase.driver(uri, auth=auth)

    with driver.session() as session:
        session.run("RETURN apoc.version()").consume()  # fail fast without APOC

        logger.info("neo4j: wiping target graph")
        while True:
            deleted = session.run(
                "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(*) AS c"
            ).single()["c"]
            if not deleted:
                break

        session.run(
            "CREATE INDEX _import_id_idx IF NOT EXISTS "
            "FOR (n:_Import) ON (n._import_id)"
        ).consume()

        def flush_nodes(batch: list[dict]) -> None:
            session.run(
                """
                UNWIND $rows AS row
                CALL apoc.create.node(row.labels + ['_Import'], row.properties) YIELD node
                SET node._import_id = row.id
                RETURN count(*)
                """,
                rows=batch,
            ).consume()

        total = 0
        batch: list[dict] = []
        with gzip.open(nodes_file, "rt", encoding="utf-8") as handle:
            for raw in handle:
                batch.append(json.loads(raw))
                if len(batch) >= BATCH:
                    flush_nodes(batch)
                    total += len(batch)
                    batch = []
        if batch:
            flush_nodes(batch)
            total += len(batch)
        logger.info("neo4j: %s nodes loaded", total)

        def flush_rels(batch: list[dict]) -> None:
            session.run(
                """
                UNWIND $rows AS row
                MATCH (a:_Import {_import_id: row.start})
                MATCH (b:_Import {_import_id: row.end})
                CALL apoc.create.relationship(a, row.type, row.properties, b) YIELD rel
                RETURN count(*)
                """,
                rows=batch,
            ).consume()

        total = 0
        batch = []
        with gzip.open(rels_file, "rt", encoding="utf-8") as handle:
            for raw in handle:
                batch.append(json.loads(raw))
                if len(batch) >= BATCH:
                    flush_rels(batch)
                    total += len(batch)
                    batch = []
        if batch:
            flush_rels(batch)
            total += len(batch)
        logger.info("neo4j: %s relationships loaded", total)

        logger.info("neo4j: removing import scaffolding")
        while True:
            done = session.run(
                "MATCH (n:_Import) WITH n LIMIT 10000 "
                "REMOVE n:_Import, n._import_id RETURN count(*) AS c"
            ).single()["c"]
            if not done:
                break
        session.run("DROP INDEX _import_id_idx IF EXISTS").consume()

        nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        logger.info("neo4j: final graph has %s nodes, %s relationships", nodes, rels)
    driver.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--stores", default="elastic,postgres,neo4j")
    ap.add_argument("--es-url", default="http://localhost:9200")
    ap.add_argument("--neo4j-container", default="neo4j-apoc")
    ap.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    ap.add_argument("--neo4j-auth", default="neo4j/password123")
    ap.add_argument("--pg-host", default="localhost")
    ap.add_argument("--pg-port", type=int, default=5435)
    ap.add_argument("--pg-db", default="nutrients")
    ap.add_argument("--pg-user", default="postgres")
    ap.add_argument("--pg-password", default="postgres")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    if not args.bundle.is_dir():
        sys.exit(f"not a bundle directory: {args.bundle}")

    stores = {s.strip() for s in args.stores.split(",") if s.strip()}
    if "elastic" in stores:
        restore_elastic(args.bundle, args.es_url)
    if "postgres" in stores:
        restore_postgres(args.bundle, dict(
            host=args.pg_host, port=args.pg_port, dbname=args.pg_db,
            user=args.pg_user, password=args.pg_password,
        ))
    if "neo4j" in stores:
        if (args.bundle / "neo4j.dump").exists():
            restore_neo4j_native(args.bundle, args.neo4j_container)
        else:
            logger.info("neo4j: no neo4j.dump, falling back to NDJSON over Bolt")
            user, _, password = args.neo4j_auth.partition("/")
            restore_neo4j_ndjson(args.bundle, args.neo4j_uri, (user, password))
    logger.info("restore complete")


if __name__ == "__main__":
    main()
