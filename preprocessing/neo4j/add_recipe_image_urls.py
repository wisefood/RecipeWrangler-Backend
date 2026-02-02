import argparse
import json
import os
from pathlib import Path

from neo4j import GraphDatabase
from tqdm import tqdm


# Purpose: Populate Recipe.image_url in Neo4j from Recipe1M layer2+ images.

def _connect():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "")
    auth = None
    if password and password.lower() != "none":
        auth = (user, password)
    return GraphDatabase.driver(uri, auth=auth)


def _iter_layer2_items(path: Path, pbar: tqdm, chunk_size: int = 1024 * 1024):
    decoder = json.JSONDecoder()
    started = False
    buf = ""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            pbar.update(len(chunk))
            buf += chunk
            if not started:
                idx = buf.find("[")
                if idx == -1:
                    if len(buf) > 1024:
                        buf = buf[-1024:]
                    continue
                buf = buf[idx + 1 :]
                started = True

            while True:
                i = 0
                while i < len(buf) and buf[i].isspace():
                    i += 1
                if i < len(buf) and buf[i] == ",":
                    i += 1
                    while i < len(buf) and buf[i].isspace():
                        i += 1
                if i:
                    buf = buf[i:]
                if not buf:
                    break
                if buf[0] == "]":
                    return
                try:
                    obj, end = decoder.raw_decode(buf)
                except json.JSONDecodeError:
                    break
                yield obj
                buf = buf[end:]


def _write_batch(session, rows):
    result = session.run(
        """
        UNWIND $rows AS row
        MATCH (r:Recipe {recipe_id: row.id})
        SET r.image_url = row.url
        RETURN count(r) AS matched
        """,
        rows=rows,
    )
    record = result.single()
    return int(record["matched"]) if record else 0


def main():
    parser = argparse.ArgumentParser(
        description="Set Recipe.image_url from Recipe1M layer2+ first image.",
    )
    parser.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    layer2_path = Path(args.layer2_path)
    total_bytes = layer2_path.stat().st_size

    driver = _connect()
    matched_total = 0
    attempted_total = 0

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            with driver.session() as session:
                rows = []
                for item in _iter_layer2_items(layer2_path, pbar):
                    items_pbar.update(1)
                    images = item.get("images") or []
                    if not images:
                        continue
                    url = images[0].get("url") if images[0] else None
                    if not url:
                        continue
                    recipe_id = item.get("id")
                    if not recipe_id:
                        continue
                    rows.append({"id": recipe_id, "url": url})
                    attempted_total += 1
                    if len(rows) >= args.batch_size:
                        if not args.dry_run:
                            matched_total += _write_batch(session, rows)
                        rows.clear()
                if rows and not args.dry_run:
                    matched_total += _write_batch(session, rows)

    driver.close()
    print(f"Attempted: {attempted_total}, matched: {matched_total}")


if __name__ == "__main__":
    main()
