#!/usr/bin/env python3
"""
Export Elasticsearch 'recipes' index to NDJSON using scroll API.
Streams all documents to avoid memory issues with large indices.
"""

import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

ES_HOST = os.environ.get("ES_HOST", "http://localhost:9200")
INDEX_NAME = "recipes"
SCROLL_TIMEOUT = "2m"
BATCH_SIZE = 1000
EXPORT_DIR = "/home/karvanitis/RecipeWrangler-Backend/data/exports/db_dumps_20260423_141753"
OUTPUT_FILE = os.path.join(EXPORT_DIR, "elasticsearch_recipes_export.ndjson")

def es_request(path, method="GET", body=None):
    url = f"{ES_HOST}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        print(f"ERROR: Elasticsearch request failed: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    os.makedirs(EXPORT_DIR, exist_ok=True)

    # Initialize scroll
    print(f"[{datetime.now(timezone.utc).isoformat()}] Initializing scroll for index '{INDEX_NAME}'...")
    init_resp = es_request(
        f"/{INDEX_NAME}/_search?scroll={SCROLL_TIMEOUT}&size={BATCH_SIZE}",
        method="POST",
        body={"sort": ["_doc"]}
    )
    scroll_id = init_resp.get("_scroll_id")
    total_hits = init_resp["hits"]["total"]
    total_docs = total_hits["value"] if isinstance(total_hits, dict) else total_hits
    print(f"Total documents to export: {total_docs:,}")

    exported = 0
    start_time = datetime.now(timezone.utc)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        while True:
            hits = init_resp["hits"]["hits"]
            if not hits:
                break

            for hit in hits:
                doc = {
                    "_index": hit["_index"],
                    "_id": hit["_id"],
                    "_source": hit["_source"]
                }
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
                exported += 1

            if exported % 10000 == 0:
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                rate = exported / elapsed if elapsed > 0 else 0
                print(f"  Exported {exported:,} / {total_docs:,} docs ({rate:.0f} docs/sec)")

            # Fetch next batch
            init_resp = es_request("/_search/scroll", method="POST", body={
                "scroll": SCROLL_TIMEOUT,
                "scroll_id": scroll_id
            })
            scroll_id = init_resp.get("_scroll_id")

    # Clear scroll
    try:
        es_request("/_search/scroll", method="DELETE", body={"scroll_id": scroll_id})
    except Exception:
        pass

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    file_size = os.path.getsize(OUTPUT_FILE)
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Export complete!")
    print(f"  File: {OUTPUT_FILE}")
    print(f"  Documents exported: {exported:,}")
    print(f"  File size: {file_size / (1024**3):.2f} GB ({file_size:,} bytes)")
    print(f"  Duration: {elapsed:.1f} seconds ({exported/elapsed:.0f} docs/sec)")

    # Verify count
    if exported != total_docs:
        print(f"WARNING: Exported count ({exported}) does not match expected ({total_docs})", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
