#!/usr/bin/env python3
"""Create timestamped sync bundles under data_to_send/dumps/<timestamp>/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DUMPS_ROOT = REPO_ROOT / "data_to_send" / "dumps"
DEFAULT_COMPONENTS = ["neo4j", "postgres", "elasticsearch", "assets"]


def _run(cmd: list[str], *, cwd: Path | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=capture,
        check=True,
    )


def _run_shell(command: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        shell=True,
        capture_output=True,
        check=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size_human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


def _docker_ps(name: str) -> bool:
    result = _run(["docker", "ps", "--format", "{{.Names}}"], capture=True)
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _docker_inspect_json(name: str) -> dict[str, Any]:
    result = _run(["docker", "inspect", name], capture=True)
    payload = json.loads(result.stdout)
    if not payload:
        raise RuntimeError(f"docker inspect returned no data for {name}")
    return payload[0]


def _write_latest(pointer: Path, text: str) -> None:
    pointer.write_text(text + "\n", encoding="utf-8")


def _es_request(base_url: str, path: str, *, method: str = "GET", body: Any | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(f"{base_url.rstrip('/')}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Elasticsearch request failed for {path}: {exc}") from exc


def export_postgres(bundle_dir: Path) -> dict[str, Any]:
    target = bundle_dir / "postgres"
    target.mkdir(parents=True, exist_ok=True)

    container = os.getenv("NUTRITION_CONTAINER", "rag-postgres")
    db = os.getenv("NUTRITION_DB", "rag")
    user = os.getenv("NUTRITION_USER", "rag")
    password = os.getenv("NUTRITION_PASSWORD", "rag")

    filename = f"postgres_{db}_{bundle_dir.name}.dump"
    out_path = target / filename

    with out_path.open("wb") as f:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={password}",
                container,
                "pg_dump",
                "-U",
                user,
                "-d",
                db,
                "-Fc",
                "--no-owner",
                "--no-privileges",
            ],
            stdout=f,
            stderr=subprocess.PIPE,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))

    counts_sql = (
        "SELECT 'nutrients-ingredients-hungarian', count(*) FROM \"nutrients-ingredients-hungarian\" "
        "UNION ALL SELECT 'nutrients-ingredients-irish', count(*) FROM \"nutrients-ingredients-irish\" "
        "UNION ALL SELECT 'nutrients-ingredients-usda', count(*) FROM \"nutrients-ingredients-usda\" "
        "UNION ALL SELECT 'nutrients-recipe-profiles', count(*) FROM \"nutrients-recipe-profiles\" "
        "UNION ALL SELECT 'pipeline_static_data', count(*) FROM pipeline_static_data "
        "UNION ALL SELECT 'structured_table_schemas', count(*) FROM structured_table_schemas "
        "UNION ALL SELECT 'structured_tables', count(*) FROM structured_tables "
        "ORDER BY 1;"
    )
    result = _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={password}",
            container,
            "psql",
            "-U",
            user,
            "-d",
            db,
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            counts_sql,
        ],
        capture=True,
    )
    record_counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, count = line.split("\t", 1)
        record_counts[name] = int(count)

    return {
        "path": str(out_path.relative_to(REPO_ROOT)),
        "record_counts": record_counts,
    }


def export_elasticsearch(bundle_dir: Path) -> dict[str, Any]:
    target = bundle_dir / "elasticsearch"
    target.mkdir(parents=True, exist_ok=True)

    base_url = os.getenv("ELASTIC_URL", "http://localhost:9200")
    index = os.getenv("ELASTIC_INDEX", "recipes")

    mapping_path = target / f"elasticsearch_{index}_mapping_{bundle_dir.name}.json"
    settings_path = target / f"elasticsearch_{index}_settings_{bundle_dir.name}.json"
    ndjson_path = target / f"elasticsearch_{index}_{bundle_dir.name}.ndjson"

    mapping_path.write_text(json.dumps(_es_request(base_url, f"/{index}/_mapping")), encoding="utf-8")
    settings_path.write_text(
        json.dumps(_es_request(base_url, f"/{index}/_settings?include_defaults=false")),
        encoding="utf-8",
    )

    scroll = "2m"
    batch_size = 2000
    response = _es_request(
        base_url,
        f"/{index}/_search?scroll={scroll}&size={batch_size}",
        method="POST",
        body={"sort": ["_doc"]},
    )
    scroll_id = response.get("_scroll_id")
    total_obj = response.get("hits", {}).get("total", 0)
    total_docs = total_obj.get("value", total_obj) if isinstance(total_obj, dict) else int(total_obj)
    exported = 0

    with ndjson_path.open("w", encoding="utf-8") as f:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                payload = {
                    "_index": hit["_index"],
                    "_id": hit["_id"],
                    "_source": hit.get("_source", {}),
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                exported += 1
            response = _es_request(
                base_url,
                "/_search/scroll",
                method="POST",
                body={"scroll": scroll, "scroll_id": scroll_id},
            )
            scroll_id = response.get("_scroll_id", scroll_id)

    try:
        if scroll_id:
            _es_request(base_url, "/_search/scroll", method="DELETE", body={"scroll_id": scroll_id})
    except Exception:
        pass

    if exported != total_docs:
        raise RuntimeError(f"Elasticsearch export mismatch: exported={exported} expected={total_docs}")

    return {
        "path": str(ndjson_path.relative_to(REPO_ROOT)),
        "mapping_path": str(mapping_path.relative_to(REPO_ROOT)),
        "settings_path": str(settings_path.relative_to(REPO_ROOT)),
        "record_counts": {"documents": exported},
    }


def export_assets(bundle_dir: Path) -> dict[str, Any]:
    target = bundle_dir / "assets"
    target.mkdir(parents=True, exist_ok=True)

    source_dir = REPO_ROOT / "data" / "Irish_SafeFood" / "images"
    out_path = target / f"irish_safefood_images_{bundle_dir.name}.tar.gz"

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(source_dir, arcname="images")

    file_count = sum(1 for _ in source_dir.iterdir() if _.is_file())
    return {
        "path": str(out_path.relative_to(REPO_ROOT)),
        "record_counts": {"entries": file_count + 1, "image_files": file_count},
    }


def export_neo4j(bundle_dir: Path) -> dict[str, Any]:
    target = bundle_dir / "neo4j"
    target.mkdir(parents=True, exist_ok=True)
    # The Neo4j Docker image runs neo4j-admin as the neo4j user, which must be
    # able to write into this host-created bind mount.
    target.chmod(0o777)

    container = os.getenv("NEO4J_CONTAINER", "neo4j-apoc")
    db = os.getenv("NEO4J_DATABASE", "neo4j")

    inspect = _docker_inspect_json(container)
    image = inspect["Config"]["Image"]
    mounts = inspect.get("Mounts", [])
    data_volume = None
    for mount in mounts:
        if mount.get("Destination") == "/data":
            data_volume = mount.get("Name")
            break
    if not data_volume:
        raise RuntimeError(f"Could not find /data volume for container {container}")

    out_path = target / f"neo4j_{bundle_dir.name}.dump"
    raw_name = f"{db}.dump"

    was_running = _docker_ps(container)
    if was_running:
        _run(["docker", "stop", container], capture=True)

    try:
        _run(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "NEO4J_ACCEPT_LICENSE_AGREEMENT=yes",
                "-v",
                f"{data_volume}:/data",
                "-v",
                f"{target.resolve()}:/dumps",
                image,
                "/var/lib/neo4j/bin/neo4j-admin",
                "database",
                "dump",
                db,
                "--to-path=/dumps",
                "--overwrite-destination=true",
            ],
            capture=True,
        )
        generated = target / raw_name
        if not generated.exists():
            raise RuntimeError(f"Expected dump file {generated} was not created")
        generated.rename(out_path)
    finally:
        if was_running:
            _run(["docker", "start", container], capture=True)
            password = os.getenv("NEO4J_PASSWORD", "password123")
            for _ in range(30):
                probe = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "/var/lib/neo4j/bin/cypher-shell",
                        "-u",
                        "neo4j",
                        "-p",
                        password,
                        "RETURN 1 AS ok",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if probe.returncode == 0:
                    break
                time.sleep(2)
            else:
                raise RuntimeError(f"Neo4j container {container} did not become ready after restart")

    password = os.getenv("NEO4J_PASSWORD", "password123")
    query = "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() RETURN nodes, count(r) AS relationships"
    result = _run(
        [
            "docker",
            "exec",
            container,
            "/var/lib/neo4j/bin/cypher-shell",
            "-u",
            "neo4j",
            "-p",
            password,
            query,
        ],
        capture=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"Unexpected Neo4j query output: {result.stdout}")
    counts_line = lines[-1]
    nodes_str, relationships_str = [part.strip() for part in counts_line.split(",", 1)]

    recipes_result = _run(
        [
            "docker",
            "exec",
            container,
            "/var/lib/neo4j/bin/cypher-shell",
            "-u",
            "neo4j",
            "-p",
            password,
            "MATCH (r:Recipe) RETURN count(r) AS recipes",
        ],
        capture=True,
    )
    recipe_lines = [line.strip() for line in recipes_result.stdout.splitlines() if line.strip()]
    if len(recipe_lines) < 2:
        raise RuntimeError(f"Unexpected Neo4j recipe count output: {recipes_result.stdout}")

    return {
        "path": str(out_path.relative_to(REPO_ROOT)),
        "record_counts": {
            "nodes": int(nodes_str),
            "relationships": int(relationships_str),
            "recipes": int(recipe_lines[-1]),
        },
    }


def write_metadata(bundle_dir: Path, exported: dict[str, dict[str, Any]]) -> None:
    metadata_dir = bundle_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    readme_path = metadata_dir / f"RESTORE_README_{bundle_dir.name}.md"
    manifest_path = metadata_dir / f"manifest_{bundle_dir.name}.json"
    checksums_path = metadata_dir / f"checksums_{bundle_dir.name}.sha256"

    restore_lines = [
        f"# RecipeWrangler Sync Export {bundle_dir.name}",
        "",
        f"Generated from `{REPO_ROOT}` on UTC timestamp `{bundle_dir.name}`.",
        "",
        "## Folder Layout",
        "",
        "- `neo4j/`: Neo4j database dump.",
        "- `postgres/`: Postgres database dump.",
        "- `elasticsearch/`: Elasticsearch index mapping/settings and document export.",
        "- `assets/`: file assets referenced by database records.",
        "- `metadata/`: manifest, checksums, and this restore note.",
        "",
        "## Restore Sketch",
        "",
    ]

    if "postgres" in exported:
        restore_lines.extend(
            [
                "Postgres:",
                "```bash",
                f"createdb rag",
                f"pg_restore -d rag --no-owner --no-privileges postgres/postgres_rag_{bundle_dir.name}.dump",
                "```",
                "",
            ]
        )
    if "neo4j" in exported:
        restore_lines.extend(
            [
                "Neo4j:",
                "```bash",
                f"neo4j-admin database load neo4j --from-path=neo4j --overwrite-destination=true",
                "```",
                "Run this while the target Neo4j database is offline/not mounted.",
                "",
            ]
        )
    if "elasticsearch" in exported:
        restore_lines.extend(
            [
                "Elasticsearch:",
                "```bash",
                f"uv run python scripts/elasticsearch/restore_recipes_dump.py \\",
                f"  --mapping {exported['elasticsearch']['mapping_path']} \\",
                f"  --input {exported['elasticsearch']['path']} \\",
                "  --es-url http://localhost:9200 \\",
                "  --index recipes \\",
                "  --recreate-index",
                "```",
                "",
            ]
        )
    if "assets" in exported:
        restore_lines.extend(
            [
                "Assets:",
                "```bash",
                f"tar -xzf assets/irish_safefood_images_{bundle_dir.name}.tar.gz -C data/Irish_SafeFood",
                "```",
                "",
            ]
        )

    readme_path.write_text("\n".join(restore_lines), encoding="utf-8")

    manifest_files: dict[str, dict[str, Any]] = {}
    checksum_inputs: list[Path] = []
    for component, info in exported.items():
        keys = ["path", "mapping_path", "settings_path"]
        for key in keys:
            rel = info.get(key)
            if not rel:
                continue
            path = REPO_ROOT / rel
            checksum_inputs.append(path)
            entry = {
                "size_bytes": path.stat().st_size,
                "size_human": _size_human(path.stat().st_size),
                "record_counts": info.get("record_counts", {}),
            }
            manifest_files[rel] = entry

    checksum_inputs.extend([readme_path, manifest_path])

    manifest = {
        "export_id": f"db_sync_{bundle_dir.name}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": "RecipeWrangler-Backend",
        "export_dir": str(bundle_dir.relative_to(REPO_ROOT)),
        "components": sorted(exported),
        "files": manifest_files,
        "verification": {
            "checksum_algorithm": "SHA-256",
            "checksums_file": str(checksums_path.relative_to(REPO_ROOT)),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    with checksums_path.open("w", encoding="utf-8") as f:
        for path in sorted(set(checksum_inputs), key=lambda p: str(p)):
            f.write(f"{_sha256(path)}  {path.relative_to(bundle_dir)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export timestamped sync bundles under data_to_send/dumps/<timestamp>/")
    parser.add_argument(
        "--components",
        nargs="+",
        choices=["neo4j", "postgres", "elasticsearch", "assets"],
        default=DEFAULT_COMPONENTS,
        help="Components to export. Default: all.",
    )
    parser.add_argument(
        "--timestamp",
        default=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        help="Timestamp folder name to use. Default: current UTC timestamp.",
    )
    args = parser.parse_args()

    bundle_dir = DUMPS_ROOT / args.timestamp
    bundle_dir.mkdir(parents=True, exist_ok=False)

    exported: dict[str, dict[str, Any]] = {}
    for component in args.components:
        if component == "neo4j":
            exported[component] = export_neo4j(bundle_dir)
        elif component == "postgres":
            exported[component] = export_postgres(bundle_dir)
        elif component == "elasticsearch":
            exported[component] = export_elasticsearch(bundle_dir)
        elif component == "assets":
            exported[component] = export_assets(bundle_dir)

    write_metadata(bundle_dir, exported)
    _write_latest(DUMPS_ROOT / "LATEST_EXPORT_TS", args.timestamp)
    _write_latest(DUMPS_ROOT / "LATEST_EXPORT_PATH", str(bundle_dir.relative_to(REPO_ROOT)))
    print(bundle_dir)


if __name__ == "__main__":
    main()
