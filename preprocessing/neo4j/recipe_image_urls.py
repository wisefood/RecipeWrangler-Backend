import argparse
import csv
import json
import os
import subprocess
import socket
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase
from tqdm import tqdm


def _connect():
    load_dotenv()
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


def _normalize_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _url_works(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, ValueError):
        pass
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Range", "bytes=0-1023")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, ValueError):
        return False


def _pick_image_url(images: list[dict], validate: bool, timeout: float) -> str | None:
    urls = []
    for image in images:
        if not image:
            continue
        url = image.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    if not urls:
        return None
    if not validate:
        for url in urls:
            if url.startswith("https://"):
                return url
        return urls[0]
    for url in urls:
        if _url_works(url, timeout):
            return url
    return None


def _fetch_missing_ids(session) -> set[str]:
    result = session.run(
        """
        MATCH (r:Recipe)
        WHERE (r.image_url IS NULL OR r.image_url = '')
        RETURN r.recipe_id AS recipe_id, r.id AS legacy_id
        """
    )
    missing: set[str] = set()
    for record in result:
        rid = _normalize_id(record.get("recipe_id"))
        if rid:
            missing.add(rid)
        legacy = _normalize_id(record.get("legacy_id"))
        if legacy:
            missing.add(legacy)
    return missing


def _fetch_with_images(session) -> list[tuple[str, str]]:
    result = session.run(
        """
        MATCH (r:Recipe)
        WHERE r.image_url IS NOT NULL AND r.image_url <> ''
        RETURN r.recipe_id AS recipe_id, r.id AS legacy_id, r.image_url AS image_url
        """
    )
    rows: list[tuple[str, str]] = []
    for record in result:
        rid = _normalize_id(record.get("recipe_id")) or _normalize_id(record.get("legacy_id"))
        url = record.get("image_url")
        if rid and isinstance(url, str) and url.strip():
            rows.append((rid, url.strip()))
    return rows


def _log_missing_summary(session) -> None:
    result = session.run(
        """
        MATCH (r:Recipe)
        RETURN count(r) AS total,
               count(r.image_url) AS with_image,
               count(CASE WHEN r.image_url IS NULL OR r.image_url = '' THEN 1 END) AS missing
        """
    )
    record = result.single()
    if record:
        print(
            "Recipe counts:",
            f"total={record['total']}",
            f"with_image={record['with_image']}",
            f"missing={record['missing']}",
        )


def _write_batch(session, rows):
    result = session.run(
        """
        UNWIND $rows AS row
        MATCH (r:Recipe)
        WHERE r.recipe_id = row.id OR r.id = row.id
        SET r.image_url = row.url
        RETURN count(r) AS matched
        """,
        rows=rows,
    )
    record = result.single()
    return int(record["matched"]) if record else 0


def extract_csv(
    layer2_path: Path,
    out_path: Path,
    validate: bool,
    timeout: float,
    https_only: bool,
) -> None:
    total_bytes = layer2_path.stat().st_size
    attempted_total = 0
    written_total = 0
    checked_total = 0

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            with out_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["recipe_id", "image_url"])
                for item in _iter_layer2_items(layer2_path, pbar):
                    items_pbar.update(1)
                    images = item.get("images") or []
                    if not images:
                        continue
                    checked_total += 1
                    if https_only:
                        url = None
                        for image in images:
                            if not image:
                                continue
                            candidate = image.get("url")
                            if isinstance(candidate, str) and candidate.strip().startswith("https://"):
                                url = candidate.strip()
                                break
                    else:
                        url = _pick_image_url(images, validate, timeout)
                    if not url:
                        continue
                    recipe_id = item.get("id")
                    if not recipe_id:
                        continue
                    attempted_total += 1
                    writer.writerow([recipe_id, url])
                    written_total += 1

    print(f"Attempted: {attempted_total}, written: {written_total}, checked: {checked_total}")
    print(f"Wrote: {out_path}")


def copy_to_docker(out_path: Path, container: str, import_dir: str) -> None:
    container_path = f"{container}:{import_dir}/{out_path.name}"
    subprocess.run(
        ["docker", "cp", str(out_path), container_path],
        check=True,
    )
    print(f"Copied to: {container_path}")


def backfill_missing(layer2_path: Path, batch_size: int, dry_run: bool, validate: bool, timeout: float) -> None:
    total_bytes = layer2_path.stat().st_size
    driver = _connect()
    matched_total = 0
    attempted_total = 0
    found_total = 0
    checked_total = 0

    with driver.session() as session:
        _log_missing_summary(session)
        missing_ids = _fetch_missing_ids(session)

    if not missing_ids:
        print("No missing image_url values found.")
        driver.close()
        return
    print(f"Missing image_url recipes: {len(missing_ids)}")

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            with driver.session() as session:
                rows = []
                for item in _iter_layer2_items(layer2_path, pbar):
                    items_pbar.update(1)
                    recipe_id = _normalize_id(item.get("id"))
                    if not recipe_id or recipe_id not in missing_ids:
                        continue
                    images = item.get("images") or []
                    if not images:
                        continue
                    checked_total += 1
                    url = _pick_image_url(images, validate, timeout)
                    if not url:
                        continue
                    rows.append({"id": recipe_id, "url": url})
                    attempted_total += 1
                    found_total += 1
                    missing_ids.discard(recipe_id)
                    if len(rows) >= batch_size:
                        if not dry_run:
                            matched_total += _write_batch(session, rows)
                        rows.clear()
                if rows and not dry_run:
                    matched_total += _write_batch(session, rows)

    driver.close()
    print(
        f"Attempted: {attempted_total}, matched: {matched_total}, found: {found_total}, checked: {checked_total}"
    )


def repair_broken(layer2_path: Path, batch_size: int, dry_run: bool, timeout: float) -> None:
    total_bytes = layer2_path.stat().st_size
    driver = _connect()
    matched_total = 0
    checked_total = 0
    broken_total = 0
    fixed_total = 0

    with driver.session() as session:
        _log_missing_summary(session)
        with_images = _fetch_with_images(session)

    if not with_images:
        print("No recipes with image_url found.")
        driver.close()
        return

    broken_ids: set[str] = set()
    for rid, url in tqdm(with_images, desc="Validating current image_url", unit="recipes"):
        checked_total += 1
        if not _url_works(url, timeout):
            broken_ids.add(rid)
            broken_total += 1

    if not broken_ids:
        print("No broken image_url values found.")
        driver.close()
        return
    print(f"Broken image_url recipes: {broken_total}")

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            with driver.session() as session:
                rows = []
                for item in _iter_layer2_items(layer2_path, pbar):
                    items_pbar.update(1)
                    recipe_id = _normalize_id(item.get("id"))
                    if not recipe_id or recipe_id not in broken_ids:
                        continue
                    images = item.get("images") or []
                    if not images:
                        continue
                    url = _pick_image_url(images, validate=True, timeout=timeout)
                    if not url:
                        continue
                    rows.append({"id": recipe_id, "url": url})
                    fixed_total += 1
                    broken_ids.discard(recipe_id)
                    if len(rows) >= batch_size:
                        if not dry_run:
                            matched_total += _write_batch(session, rows)
                        rows.clear()
                if rows and not dry_run:
                    matched_total += _write_batch(session, rows)

    driver.close()
    print(
        "Repair summary:",
        f"checked={checked_total}",
        f"broken={broken_total}",
        f"fixed_candidates={fixed_total}",
        f"matched={matched_total}",
    )


def _fetch_http_image_ids(session) -> set[str]:
    result = session.run(
        """
        MATCH (r:Recipe)
        WHERE r.image_url STARTS WITH 'http://'
        RETURN r.recipe_id AS recipe_id, r.id AS legacy_id
        """
    )
    ids: set[str] = set()
    for record in result:
        rid = _normalize_id(record.get("recipe_id")) or _normalize_id(record.get("legacy_id"))
        if rid:
            ids.add(rid)
    return ids


def prefer_https(layer2_path: Path, batch_size: int, dry_run: bool) -> None:
    total_bytes = layer2_path.stat().st_size
    driver = _connect()
    matched_total = 0
    updated_total = 0

    with driver.session() as session:
        http_ids = _fetch_http_image_ids(session)

    if not http_ids:
        print("No http:// image_url values found.")
        driver.close()
        return
    print(f"Recipes with http:// image_url: {len(http_ids)}")

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            with driver.session() as session:
                rows = []
                for item in _iter_layer2_items(layer2_path, pbar):
                    items_pbar.update(1)
                    recipe_id = _normalize_id(item.get("id"))
                    if not recipe_id or recipe_id not in http_ids:
                        continue
                    images = item.get("images") or []
                    if not images:
                        continue
                    https_url = None
                    for image in images:
                        if not image:
                            continue
                        url = image.get("url")
                        if isinstance(url, str) and url.strip().startswith("https://"):
                            https_url = url.strip()
                            break
                    if not https_url:
                        continue
                    rows.append({"id": recipe_id, "url": https_url})
                    updated_total += 1
                    http_ids.discard(recipe_id)
                    if len(rows) >= batch_size:
                        if not dry_run:
                            matched_total += _write_batch(session, rows)
                        rows.clear()
                if rows and not dry_run:
                    matched_total += _write_batch(session, rows)

    driver.close()
    print(
        "Prefer-https summary:",
        f"updated_candidates={updated_total}",
        f"matched={matched_total}",
    )


def load_csv_into_neo4j(container: str, password: str, filename: str) -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "cypher-shell",
            "-u",
            "neo4j",
            "-p",
            password,
            (
                "LOAD CSV WITH HEADERS FROM 'file:///"
                + filename
                + "' AS row "
                "MATCH (r:Recipe) "
                "WHERE r.recipe_id = row.recipe_id OR r.id = row.recipe_id "
                "SET r.image_url = row.image_url"
            ),
        ],
        check=True,
    )


def extract_and_load_https(
    layer2_path: Path,
    out_path: Path,
    container: str,
    import_dir: str,
    password: str,
) -> None:
    extract_csv(layer2_path, out_path, validate=False, timeout=0.0, https_only=True)
    copy_to_docker(out_path, container, import_dir)
    load_csv_into_neo4j(container, password, out_path.name)


def report_missing(layer2_path: Path, sample: int) -> None:
    total_bytes = layer2_path.stat().st_size
    driver = _connect()
    with driver.session() as session:
        missing_ids = _fetch_missing_ids(session)

    if not missing_ids:
        print("No missing image_url values found.")
        driver.close()
        return

    seen_missing = 0
    with_images = 0
    sample_with_images: list[str] = []
    sample_no_images: list[str] = []

    with tqdm(total=total_bytes, unit="B", unit_scale=True) as pbar:
        with tqdm(unit="recipes") as items_pbar:
            for item in _iter_layer2_items(layer2_path, pbar):
                items_pbar.update(1)
                recipe_id = _normalize_id(item.get("id"))
                if not recipe_id or recipe_id not in missing_ids:
                    continue
                seen_missing += 1
                images = item.get("images") or []
                if images and images[0] and images[0].get("url"):
                    with_images += 1
                    if len(sample_with_images) < sample:
                        sample_with_images.append(recipe_id)
                else:
                    if len(sample_no_images) < sample:
                        sample_no_images.append(recipe_id)

    driver.close()
    total_missing = len(missing_ids)
    print(f"Missing in Neo4j: {total_missing}")
    print(f"Present in layer2+: {seen_missing}")
    print(f"Present with images: {with_images}")
    if sample_with_images:
        print(f"Sample missing IDs WITH images: {sample_with_images}")
    if sample_no_images:
        print(f"Sample missing IDs with NO images: {sample_no_images}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recipe1M image URL utilities (extract, backfill, report).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract-csv", help="Extract recipe_id+image_url to CSV.")
    extract.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    extract.add_argument(
        "--out",
        default="/tmp/recipe_image_urls.csv",
        help="Output CSV path.",
    )
    extract.add_argument(
        "--validate-urls",
        action="store_true",
        help="Validate URLs with network requests (slower).",
    )
    extract.add_argument(
        "--https-only",
        action="store_true",
        help="Only include https:// URLs (fast, no validation).",
    )
    extract.add_argument(
        "--timeout",
        type=float,
        default=2.5,
        help="Timeout in seconds for URL validation.",
    )
    extract.add_argument(
        "--docker-container",
        default="",
        help="Optional: docker container name to copy CSV into.",
    )
    extract.add_argument(
        "--docker-import-dir",
        default="/import",
        help="Import directory inside the Neo4j container.",
    )
    extract.add_argument(
        "--docker-password",
        default=os.getenv("NEO4J_PASSWORD", "password123"),
        help="Neo4j password for cypher-shell when loading CSV.",
    )
    extract.add_argument(
        "--load-into-neo4j",
        action="store_true",
        help="After extract, copy into docker and LOAD CSV into Neo4j.",
    )

    backfill = subparsers.add_parser("backfill-missing", help="Backfill missing image_url in Neo4j.")
    backfill.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    backfill.add_argument("--batch-size", type=int, default=1000)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument(
        "--validate-urls",
        action="store_true",
        help="Validate URLs with network requests (slower).",
    )
    backfill.add_argument(
        "--timeout",
        type=float,
        default=2.5,
        help="Timeout in seconds for URL validation.",
    )

    report = subparsers.add_parser("report-missing", help="Report layer2+ coverage for missing image_url.")
    report.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    report.add_argument("--sample", type=int, default=20)

    repair = subparsers.add_parser(
        "repair-broken",
        help="Validate existing image_url values and replace broken URLs from layer2+.",
    )
    repair.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    repair.add_argument("--batch-size", type=int, default=1000)
    repair.add_argument("--dry-run", action="store_true")
    repair.add_argument(
        "--timeout",
        type=float,
        default=2.5,
        help="Timeout in seconds for URL validation.",
    )

    prefer = subparsers.add_parser(
        "prefer-https",
        help="Replace http:// image_url with https:// alternatives from layer2+ when available.",
    )
    prefer.add_argument(
        "--layer2-path",
        default="data/raw/recipe1m/layer2+.json",
        help="Path to layer2+.json",
    )
    prefer.add_argument("--batch-size", type=int, default=1000)
    prefer.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "extract-csv":
        out_path = Path(args.out)
        extract_csv(
            Path(args.layer2_path),
            out_path,
            args.validate_urls,
            args.timeout,
            args.https_only,
        )
        if args.docker_container:
            copy_to_docker(out_path, args.docker_container, args.docker_import_dir)
        if args.load_into_neo4j:
            if not args.docker_container:
                raise SystemExit("--load-into-neo4j requires --docker-container")
            load_csv_into_neo4j(args.docker_container, args.docker_password, out_path.name)
    elif args.command == "backfill-missing":
        backfill_missing(Path(args.layer2_path), args.batch_size, args.dry_run, args.validate_urls, args.timeout)
    elif args.command == "report-missing":
        report_missing(Path(args.layer2_path), args.sample)
    elif args.command == "repair-broken":
        repair_broken(Path(args.layer2_path), args.batch_size, args.dry_run, args.timeout)
    elif args.command == "prefer-https":
        prefer_https(Path(args.layer2_path), args.batch_size, args.dry_run)


if __name__ == "__main__":
    main()
