#!/usr/bin/env python3
"""Dump the three stores without importing recipe_wrangler.

Standalone (psycopg2 + neo4j + stdlib + docker) with connection settings read
from the repo .env. Exists because dump_all.py imports the full
recipe_wrangler package, whose venv (torch and friends) cannot always be
built on the machine taking the dump.

Outputs per bundle directory dumps/<stamp>[-label]/:
- nutrients.dump           native pg_dump -Fc of the whole database
- neo4j.dump               native neo4j-admin archive
- elastic-<alias>.{ndjson.gz,mapping.json,settings.json}
- MANIFEST.json            record counts per store

Native mechanisms are used for Neo4j and Postgres. Postgres dumps online via
a dockerized pg_dump whose major version is matched to the server.
neo4j-admin can only dump a stopped database and production cannot be
stopped, so the graph is first streamed over Bolt into a throwaway local
staging container, which is then stopped and dumped natively — the resulting
neo4j.dump loads with `neo4j-admin database load` like any other.

Usage:
  python3 scripts/maintenance/dump_all_lite.py [--label NAME] [--stores elastic,postgres,neo4j]
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("dump_all_lite")

ES_ALIASES = ("recipes", "ingredient_vectors")

STAGING_IMAGE = "neo4j:5.24-enterprise"  # same family as neo4j-docker/Dockerfile
STAGING_NAME = "neo4j-dump-staging"
# Bind mount, not a named volume: /var/lib/docker sits on the root disk,
# which is full; the repo lives on /mnt where there is room.
STAGING_DATA_DIR = REPO_ROOT / ".neo4j-dump-staging"
STAGING_BOLT_PORT = 27687
BATCH = 1000


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


ENV = load_env()
ES_URL = ENV.get("ELASTIC_URL", "http://localhost:19200").rstrip("/")
NEO4J_URI = ENV.get("NEO4J_URI", "bolt://localhost:17687")
NEO4J_USER, _, NEO4J_PASSWORD = ENV.get("NEO4J_AUTH", "neo4j/").partition("/")
PG = dict(
    host=ENV.get("NUTRITION_HOST", "localhost"),
    port=int(ENV.get("NUTRITION_PORT", "15432")),
    dbname=ENV.get("NUTRITION_DB", "nutrients"),
    user=ENV.get("NUTRITION_USER", "postgres"),
    password=ENV.get("NUTRITION_PASSWORD", ""),
)


def _docker(*args: str, env: dict | None = None, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], env=env, capture_output=True, check=True, **kwargs
    )


# ── Elasticsearch ────────────────────────────────────────────────────────────

def _es(path: str, body: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"{ES_URL}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def dump_elastic(out: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for alias in ES_ALIASES:
        mapping = _es(f"/{alias}/_mapping")
        (out / f"elastic-{alias}.mapping.json").write_text(
            json.dumps(mapping, indent=2, sort_keys=True)
        )
        settings = _es(f"/{alias}/_settings")
        (out / f"elastic-{alias}.settings.json").write_text(
            json.dumps(settings, indent=2, sort_keys=True)
        )

        path = out / f"elastic-{alias}.ndjson.gz"
        written = 0
        response = _es(f"/{alias}/_search?scroll=5m&size=500", body={"sort": ["_doc"]})
        scroll_id = response.get("_scroll_id")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            while True:
                hits = response.get("hits", {}).get("hits", [])
                if not hits:
                    break
                for hit in hits:
                    handle.write(
                        json.dumps({"_id": hit["_id"], "_source": hit["_source"]}) + "\n"
                    )
                    written += 1
                response = _es(
                    "/_search/scroll",
                    body={"scroll": "5m", "scroll_id": scroll_id},
                    method="POST",
                )
                scroll_id = response.get("_scroll_id", scroll_id)
        if scroll_id:
            try:
                _es("/_search/scroll", body={"scroll_id": scroll_id}, method="DELETE")
            except Exception:  # noqa: BLE001
                pass
        counts[alias] = written
        logger.info("elastic: %s -> %s docs (%s)", alias, written, path.name)
    return counts


# ── Postgres ─────────────────────────────────────────────────────────────────

def _psql(sql: str, image: str = "postgres:16-alpine") -> list[str]:
    """Run a query via a dockerized psql; returns non-empty output lines.

    psql only needs protocol compatibility, so any modern client image works
    for probing regardless of the server's major version.
    """
    env = os.environ.copy()
    env["PGPASSWORD"] = str(PG["password"])
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
            image,
            "psql", "-h", "127.0.0.1", "-p", str(PG["port"]),
            "-U", str(PG["user"]), "-d", str(PG["dbname"]),
            "-tA", "-F", "\t", "-c", sql,
        ],
        env=env, capture_output=True, check=True, text=True, timeout=300,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def dump_postgres(out: Path) -> dict[str, int]:
    server_version = _psql("SHOW server_version")[0].split()[0]
    tables = _psql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
    )
    counts_sql = " UNION ALL ".join(
        f"SELECT '{t}' AS t, count(*) AS c FROM \"{t}\"" for t in tables
    )
    counts: dict[str, int] = {}
    for line in _psql(counts_sql):
        name, count = line.split("\t", 1)
        counts[name] = int(count)

    major = server_version.split(".")[0]
    image = f"postgres:{major}-alpine"
    logger.info("postgres: server %s, using %s for pg_dump", server_version, image)

    docker_env = os.environ.copy()
    docker_env["PGPASSWORD"] = str(PG["password"])
    dump_path = out / "nutrients.dump"
    with dump_path.open("wb") as handle:
        subprocess.run(
            [
                "docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
                image,
                "pg_dump", "-Fc", "--no-owner", "--no-privileges",
                "-h", "127.0.0.1", "-p", str(PG["port"]),
                "-U", str(PG["user"]), "-d", str(PG["dbname"]),
            ],
            env=docker_env, stdout=handle, stderr=subprocess.PIPE, check=True,
            timeout=1800,
        )
    logger.info(
        "postgres: %s tables -> nutrients.dump (%.1f MB)",
        len(tables), dump_path.stat().st_size / 1e6,
    )
    return counts


# ── Neo4j ────────────────────────────────────────────────────────────────────

NEO4J_NODES_Q = "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props, elementId(n) AS eid"
NEO4J_RELS_Q = (
    "MATCH (a)-[r]->(b) RETURN type(r) AS type, properties(r) AS props, "
    "elementId(a) AS src, elementId(b) AS dst"
)


def _staging_cleanup() -> None:
    import shutil

    subprocess.run(["docker", "rm", "-f", STAGING_NAME], capture_output=True)
    if STAGING_DATA_DIR.exists():
        # The container writes as the neo4j user, so the host user cannot
        # delete those files directly; remove them from inside a container.
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{STAGING_DATA_DIR.resolve()}:/data",
                STAGING_IMAGE,
                "bash", "-c", "rm -rf /data/..?* /data/.[!.]* /data/*",
            ],
            capture_output=True,
        )
        shutil.rmtree(STAGING_DATA_DIR, ignore_errors=True)


def _staging_start():
    from neo4j import GraphDatabase

    _staging_cleanup()
    STAGING_DATA_DIR.mkdir(parents=True)
    STAGING_DATA_DIR.chmod(0o777)  # the image runs as the neo4j user
    _docker(
        "run", "-d", "--name", STAGING_NAME,
        "-e", "NEO4J_AUTH=none",
        "-e", "NEO4J_ACCEPT_LICENSE_AGREEMENT=yes",
        "-e", 'NEO4J_PLUGINS=["apoc"]',
        "-p", f"{STAGING_BOLT_PORT}:7687",
        "-v", f"{STAGING_DATA_DIR.resolve()}:/data",
        STAGING_IMAGE,
    )
    driver = GraphDatabase.driver(f"bolt://localhost:{STAGING_BOLT_PORT}", auth=None)
    deadline = time.monotonic() + 180
    while True:
        try:
            driver.verify_connectivity()
            with driver.session() as s:
                s.run("RETURN apoc.version()").consume()
            return driver
        except Exception:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def dump_neo4j(out: Path) -> dict[str, int]:
    """Native neo4j.dump, without stopping the source.

    Stream the graph over Bolt into a local staging container, stop it, and
    run `neo4j-admin database dump` against its (now offline) volume.
    """
    from neo4j import GraphDatabase

    source = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    counts: dict[str, int] = {}

    logger.info("neo4j: starting staging container %s (%s)", STAGING_NAME, STAGING_IMAGE)
    staging = _staging_start()
    try:
        with staging.session() as dst:
            dst.run(
                "CREATE INDEX _import_id_idx IF NOT EXISTS "
                "FOR (n:_Import) ON (n._import_id)"
            ).consume()

            def flush_nodes(rows: list[dict]) -> None:
                dst.run(
                    """
                    UNWIND $rows AS row
                    CALL apoc.create.node(row.labels + ['_Import'], row.props) YIELD node
                    SET node._import_id = row.id
                    RETURN count(*)
                    """,
                    rows=rows,
                ).consume()

            def flush_rels(rows: list[dict]) -> None:
                dst.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:_Import {_import_id: row.src})
                    MATCH (b:_Import {_import_id: row.dst})
                    CALL apoc.create.relationship(a, row.type, row.props, b) YIELD rel
                    RETURN count(*)
                    """,
                    rows=rows,
                ).consume()

            total = 0
            batch: list[dict] = []
            with source.session() as src:
                for record in src.run(NEO4J_NODES_Q):
                    batch.append(
                        {"id": record["eid"], "labels": record["labels"], "props": record["props"]}
                    )
                    if len(batch) >= BATCH:
                        flush_nodes(batch)
                        total += len(batch)
                        batch = []
            if batch:
                flush_nodes(batch)
                total += len(batch)
            counts["nodes"] = total
            logger.info("neo4j: %s nodes staged", total)

            total = 0
            batch = []
            with source.session() as src:
                for record in src.run(NEO4J_RELS_Q):
                    batch.append(
                        {
                            "type": record["type"], "props": record["props"],
                            "src": record["src"], "dst": record["dst"],
                        }
                    )
                    if len(batch) >= BATCH:
                        flush_rels(batch)
                        total += len(batch)
                        batch = []
            if batch:
                flush_rels(batch)
                total += len(batch)
            counts["relationships"] = total
            logger.info("neo4j: %s relationships staged", total)

            logger.info("neo4j: removing import scaffolding from staging")
            while True:
                done = dst.run(
                    "MATCH (n:_Import) WITH n LIMIT 10000 "
                    "REMOVE n:_Import, n._import_id RETURN count(*) AS c"
                ).single()["c"]
                if not done:
                    break
            dst.run("DROP INDEX _import_id_idx IF EXISTS").consume()

            staged_nodes = dst.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            staged_rels = dst.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            if staged_nodes != counts["nodes"] or staged_rels != counts["relationships"]:
                raise RuntimeError(
                    f"staging mismatch: staged {staged_nodes}/{staged_rels} vs "
                    f"streamed {counts['nodes']}/{counts['relationships']}"
                )
        staging.close()
        source.close()

        logger.info("neo4j: stopping staging and running neo4j-admin database dump")
        _docker("stop", STAGING_NAME)
        out.chmod(0o777)  # the image runs neo4j-admin as the neo4j user
        _docker(
            "run", "--rm",
            "-e", "NEO4J_ACCEPT_LICENSE_AGREEMENT=yes",
            "-v", f"{STAGING_DATA_DIR.resolve()}:/data",
            "-v", f"{out.resolve()}:/dumps",
            STAGING_IMAGE,
            "neo4j-admin", "database", "dump", "neo4j",
            "--to-path=/dumps", "--overwrite-destination=true",
        )
        dump_path = out / "neo4j.dump"
        if not dump_path.exists():
            raise RuntimeError("neo4j-admin did not produce neo4j.dump")
        logger.info("neo4j: neo4j.dump (%.1f MB)", dump_path.stat().st_size / 1e6)
    finally:
        _staging_cleanup()
    return counts


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stores", default="elastic,postgres,neo4j")
    ap.add_argument("--out", default=None)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else REPO_ROOT / "dumps" / (
        f"{stamp}{('-' + args.label) if args.label else ''}"
    )
    out.mkdir(parents=True, exist_ok=True)
    logger.info("dumping to %s", out)

    stores = {s.strip() for s in args.stores.split(",") if s.strip()}
    # Merge into an existing manifest so a partial bundle can be completed
    # with --out <same dir> --stores <what failed> without losing counts.
    manifest_path = out / "MANIFEST.json"
    manifest: dict[str, object] = (
        json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    )
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    manifest.setdefault("elastic_index_alias", ENV.get("ELASTIC_INDEX", "recipes"))
    manifest.setdefault("stores", {})

    try:
        if "elastic" in stores:
            manifest["stores"]["elastic"] = dump_elastic(out)
        if "postgres" in stores:
            manifest["stores"]["postgres"] = dump_postgres(out)
        if "neo4j" in stores:
            manifest["stores"]["neo4j"] = dump_neo4j(out)
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    total = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    logger.info("done: %s files, %.1f MB", len(list(out.iterdir())), total / 1e6)
    print(out)


if __name__ == "__main__":
    main()
