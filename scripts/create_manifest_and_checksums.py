#!/usr/bin/env python3
"""
Create manifest.json and generate SHA-256 checksums for all exported files.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

EXPORT_DIR = "/home/karvanitis/RecipeWrangler-Backend/data/exports/db_dumps_20260423_141753"
MANIFEST_FILE = os.path.join(EXPORT_DIR, "manifest.json")
CHECKSUMS_FILE = os.path.join(EXPORT_DIR, "checksums.sha256")

def get_file_info(filepath):
    stat = os.stat(filepath)
    return {
        "size_bytes": stat.st_size,
        "size_human": f"{stat.st_size / (1024**3):.2f} GB" if stat.st_size > 1024**3 else f"{stat.st_size / (1024**2):.2f} MB",
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    }

def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def count_ndjson_lines(filepath):
    count = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for _ in f:
            count += 1
    return count

def get_container_info():
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        containers = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split("|", 2)
                containers.append({
                    "name": parts[0],
                    "status": parts[1] if len(parts) > 1 else "unknown",
                    "ports": parts[2] if len(parts) > 2 else ""
                })
        return containers
    except Exception as e:
        return [{"error": str(e)}]

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Creating manifest and checksums...")

    files = {}
    checksums = []

    # Process each file in export directory
    for filename in sorted(os.listdir(EXPORT_DIR)):
        filepath = os.path.join(EXPORT_DIR, filename)
        if not os.path.isfile(filepath):
            continue

        print(f"  Processing {filename}...")
        info = get_file_info(filepath)
        checksum = sha256_file(filepath)
        checksums.append(f"{checksum}  {filename}")

        file_entry = {
            "size_bytes": info["size_bytes"],
            "size_human": info["size_human"],
            "sha256": checksum
        }

        # Add record counts for data files
        if filename == "neo4j_graph_export.json":
            # Count nodes and relationships in Neo4j export
            print(f"    Counting nodes/relationships in {filename}...")
            node_count = 0
            rel_count = 0
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.rstrip(","))
                        if "nodes" in obj:
                            node_count = len(obj["nodes"])
                        if "relationships" in obj:
                            rel_count = len(obj["relationships"])
                    except json.JSONDecodeError:
                        # Handle trailing commas or partial lines
                        pass
            file_entry["record_counts"] = {
                "nodes": node_count,
                "relationships": rel_count
            }

        elif filename == "elasticsearch_recipes_export.ndjson":
            print(f"    Counting lines in {filename}...")
            doc_count = count_ndjson_lines(filepath)
            file_entry["record_counts"] = {
                "documents": doc_count
            }

        files[filename] = file_entry

    # Write checksums file
    with open(CHECKSUMS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(checksums) + "\n")
    print(f"  Written {CHECKSUMS_FILE}")

    # Build manifest
    manifest = {
        "export_id": "db_dumps_20260423_141753",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": "RecipeWrangler-Backend",
        "export_type": "database_dumps",
        "databases": {
            "neo4j": {
                "export_method": "apoc_json_export",
                "native_dump_attempted": True,
                "native_dump_status": "failed_database_in_use",
                "note": "APOC JSON export used to avoid service disruption"
            },
            "elasticsearch": {
                "index": "recipes",
                "export_method": "scroll_api_ndjson"
            }
        },
        "containers_at_export": get_container_info(),
        "files": files,
        "verification": {
            "checksum_algorithm": "SHA-256",
            "checksums_file": "checksums.sha256"
        }
    }

    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"  Written {MANIFEST_FILE}")

    # Print summary
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Manifest and checksums complete!")
    print(f"  Files processed: {len(files)}")
    for fname, finfo in files.items():
        recs = finfo.get("record_counts", {})
        rec_str = ""
        if recs:
            rec_str = f" ({', '.join(f'{k}={v:,}' for k, v in recs.items())})"
        print(f"    - {fname}: {finfo['size_human']}{rec_str}")

if __name__ == "__main__":
    main()
