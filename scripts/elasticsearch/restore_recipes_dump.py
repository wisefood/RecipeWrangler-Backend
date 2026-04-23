#!/usr/bin/env python3
"""Restore an Elasticsearch recipes dump from mapping JSON and NDJSON documents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


DEFAULT_ES_URL = "http://localhost:9200"
DEFAULT_INDEX = "recipes"


def _load_mapping(mapping_path: Path, index: str) -> dict:
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Mapping file is empty or not a JSON object.")
    if index in payload:
        index_payload = payload[index]
    elif len(payload) == 1:
        index_payload = next(iter(payload.values()))
    else:
        raise ValueError(f"Mapping file does not contain index '{index}'.")
    mappings = index_payload.get("mappings")
    if not isinstance(mappings, dict):
        raise ValueError(f"Mapping file for index '{index}' has no 'mappings' object.")
    return mappings


def _chunks(items: list[str], chunk_size: int):
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore Elasticsearch recipes index from exported mapping JSON and NDJSON documents."
    )
    parser.add_argument("--mapping", type=Path, required=True, help="Path to recipes _mapping JSON export.")
    parser.add_argument("--input", type=Path, required=True, help="Path to recipes NDJSON export.")
    parser.add_argument("--es-url", type=str, default=DEFAULT_ES_URL)
    parser.add_argument("--index", type=str, default=DEFAULT_INDEX)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--recreate-index",
        action="store_true",
        help="Delete and recreate the target index using the provided mapping before import.",
    )
    args = parser.parse_args()

    base_url = args.es_url.rstrip("/")
    index_url = f"{base_url}/{args.index}"

    if args.recreate_index:
        mappings = _load_mapping(args.mapping, args.index)
        delete_resp = requests.delete(index_url, timeout=60)
        if delete_resp.status_code not in {200, 404}:
            delete_resp.raise_for_status()
        create_resp = requests.put(index_url, json={"mappings": mappings}, timeout=60)
        create_resp.raise_for_status()
        print(f"Recreated index '{args.index}'.")

    bulk_lines: list[str] = []
    total_indexed = 0
    total_failed = 0

    headers = {"Content-Type": "application/x-ndjson"}
    bulk_url = f"{base_url}/_bulk"
    line_batch_size = max(2, args.batch_size * 2)

    with args.input.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            doc_id = str(record.get("_id") or "").strip()
            source = record.get("_source")
            if not doc_id or not isinstance(source, dict):
                continue
            bulk_lines.append(json.dumps({"index": {"_index": args.index, "_id": doc_id}}, ensure_ascii=False))
            bulk_lines.append(json.dumps(source, ensure_ascii=False))

    if not bulk_lines:
        print("No valid documents found in dump.")
        return

    print(f"Starting restore into '{args.index}'...")
    for batch in _chunks(bulk_lines, line_batch_size):
        resp = requests.post(bulk_url, headers=headers, data=("\n".join(batch) + "\n").encode("utf-8"), timeout=120)
        resp.raise_for_status()
        payload = resp.json()
        for item in payload.get("items", []):
            op = item.get("index") or item.get("create") or {}
            status = int(op.get("status", 500))
            if 200 <= status < 300:
                total_indexed += 1
            else:
                total_failed += 1

    refresh_resp = requests.post(f"{index_url}/_refresh", timeout=60)
    refresh_resp.raise_for_status()
    print(f"Done. indexed={total_indexed} failed={total_failed}")


if __name__ == "__main__":
    main()
