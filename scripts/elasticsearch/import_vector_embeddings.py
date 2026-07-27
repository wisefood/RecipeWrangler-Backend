#!/usr/bin/env python3
"""Import an embedding NDJSON export into a versioned Elasticsearch vector index."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
DEFAULT_INDEX = "ingredient_vectors_v1"
DEFAULT_ALIAS = os.getenv("ELASTIC_VECTOR_INDEX", "ingredient_vectors")

INDEX_BODY = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "collection": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "document": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "metadata": {"type": "flattened"},
            "embedding_model": {"type": "keyword"},
            "embedding_revision": {"type": "keyword"},
            "vector_space": {"type": "keyword"},
            "embedding": {
                "type": "dense_vector",
                "dims": 384,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "hnsw"},
            },
        },
    },
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Elasticsearch-ready embedding NDJSON."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete the exact target index before recreating it.",
    )
    parser.add_argument(
        "--activate-alias",
        action="store_true",
        help="Atomically point --alias at the imported physical index.",
    )
    return parser.parse_args()


def _iter_records(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as input_file:
        for line_number, raw_line in enumerate(input_file, start=1):
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            source = record.get("_source")
            if not isinstance(source, dict):
                raise ValueError(f"Invalid record at line {line_number}")
            collection_name = str(source.get("collection") or "").strip()
            source_id = str(source.get("source_id") or "")
            if not collection_name or not source_id:
                raise ValueError(
                    f"Record at line {line_number} has no collection/source_id"
                )
            doc_id = hashlib.sha256(
                f"{collection_name}\0{source_id}".encode("utf-8")
            ).hexdigest()
            vector = source.get("embedding")
            if not isinstance(vector, list) or len(vector) != 384:
                raise ValueError(
                    f"Record at line {line_number} does not have a 384-dimensional embedding"
                )
            values = [float(value) for value in vector]
            if any(not math.isfinite(value) for value in values):
                raise ValueError(f"Non-finite embedding at line {line_number}")
            if not any(value != 0.0 for value in values):
                raise ValueError(f"Zero-magnitude embedding at line {line_number}")
            if source.get("vector_space") != "cosine":
                raise ValueError(
                    f"Record at line {line_number} is not marked as cosine space"
                )
            source["embedding"] = values
            yield {"_id": doc_id, "_source": source}


def _load_manifest(input_path: Path) -> dict | None:
    path = input_path.with_suffix(input_path.suffix + ".manifest.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_checksum(input_path: Path, manifest: dict | None) -> None:
    if not manifest or not manifest.get("sha256"):
        return
    digest = hashlib.sha256()
    with input_path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    expected = str(manifest["sha256"])
    if actual != expected:
        raise ValueError(f"Export checksum mismatch: expected={expected} actual={actual}")


def _create_index(base_url: str, index: str, recreate: bool) -> None:
    index_url = f"{base_url}/{index}"
    exists = requests.head(index_url, timeout=10).status_code == 200
    if exists and recreate:
        response = requests.delete(index_url, timeout=60)
        response.raise_for_status()
        exists = False
    if exists:
        return
    response = requests.put(index_url, json=INDEX_BODY, timeout=60)
    response.raise_for_status()
    print(f"created index={index}")


def _send_bulk(base_url: str, index: str, records: list[dict]) -> tuple[int, int]:
    lines: list[str] = []
    for record in records:
        lines.append(json.dumps({"index": {"_index": index, "_id": record["_id"]}}))
        lines.append(json.dumps(record["_source"], ensure_ascii=False, separators=(",", ":")))
    response = requests.post(
        f"{base_url}/_bulk",
        headers={"Content-Type": "application/x-ndjson"},
        data=("\n".join(lines) + "\n").encode("utf-8"),
        timeout=180,
    )
    if not response.ok:
        print(f"bulk request failed status={response.status_code} body={response.text[:2000]}")
    response.raise_for_status()
    indexed = 0
    failed = 0
    for item in response.json().get("items", []):
        operation = item.get("index") or {}
        if 200 <= int(operation.get("status", 500)) < 300:
            indexed += 1
        else:
            failed += 1
            if failed <= 5:
                print(f"bulk failure: {operation.get('error')}")
    return indexed, failed


def _activate_alias(base_url: str, index: str, alias: str) -> None:
    existing_response = requests.get(f"{base_url}/_alias/{alias}", timeout=30)
    if existing_response.status_code == 404:
        existing_indices: list[str] = []
    else:
        existing_response.raise_for_status()
        existing_indices = list(existing_response.json())
    actions = [
        {"remove": {"index": old_index, "alias": alias}}
        for old_index in existing_indices
        if old_index != index
    ]
    actions.append({"add": {"index": index, "alias": alias}})
    response = requests.post(
        f"{base_url}/_aliases",
        json={"actions": actions},
        timeout=60,
    )
    response.raise_for_status()
    print(f"activated alias={alias} index={index}")


def _verify_collection_counts(
    base_url: str,
    index: str,
    expected: dict[str, int],
) -> None:
    response = requests.post(
        f"{base_url}/{index}/_search",
        json={
            "size": 0,
            "aggs": {
                "collections": {
                    "terms": {"field": "collection", "size": max(10, len(expected))}
                }
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    buckets = (
        response.json()
        .get("aggregations", {})
        .get("collections", {})
        .get("buckets", [])
    )
    actual = {str(bucket["key"]): int(bucket["doc_count"]) for bucket in buckets}
    normalized_expected = {str(key): int(value) for key, value in expected.items()}
    if actual != normalized_expected:
        raise RuntimeError(
            f"Elasticsearch collection counts mismatch: "
            f"actual={actual} expected={normalized_expected}"
        )
    print(f"verified collection_counts={actual}")


def main() -> None:
    args = _parse_args()
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    input_path = args.input.resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    manifest = _load_manifest(input_path)
    _verify_checksum(input_path, manifest)
    base_url = args.es_url.rstrip("/")
    _create_index(base_url, args.index, args.recreate)

    indexed = 0
    failed = 0
    batch: list[dict] = []
    for record in _iter_records(input_path):
        batch.append(record)
        if len(batch) >= args.batch_size:
            ok, bad = _send_bulk(base_url, args.index, batch)
            indexed += ok
            failed += bad
            batch.clear()
            print(f"progress indexed={indexed} failed={failed}")
    if batch:
        ok, bad = _send_bulk(base_url, args.index, batch)
        indexed += ok
        failed += bad

    requests.post(f"{base_url}/{args.index}/_refresh", timeout=60).raise_for_status()
    count_response = requests.get(f"{base_url}/{args.index}/_count", timeout=30)
    count_response.raise_for_status()
    index_count = int(count_response.json()["count"])
    expected_count = int((manifest or {}).get("total_count", indexed))
    print(
        f"done indexed={indexed} failed={failed} "
        f"index_count={index_count} expected={expected_count}"
    )
    if failed:
        raise RuntimeError(f"Elasticsearch rejected {failed} vector documents")
    if index_count != expected_count:
        raise RuntimeError(
            f"Elasticsearch count mismatch: index={index_count} expected={expected_count}"
        )
    if manifest and isinstance(manifest.get("collections"), dict):
        _verify_collection_counts(
            base_url,
            args.index,
            manifest["collections"],
        )
    if args.activate_alias:
        _activate_alias(base_url, args.index, args.alias)


if __name__ == "__main__":
    main()
