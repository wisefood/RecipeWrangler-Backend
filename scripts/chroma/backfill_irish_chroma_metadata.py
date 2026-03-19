#!/usr/bin/env python3
"""Backfill canonical_food_id metadata in the Irish nutrition Chroma collection."""

from __future__ import annotations

import argparse
import csv
import math
import re
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text

from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.nutrition_postgres import _get_config, get_connection


def _norm_name(value: object) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _iter_collection_rows(collection, page_size: int = 1000) -> Iterable[Tuple[str, str, dict]]:
    total = collection.count()
    for offset in range(0, total, page_size):
        page = collection.get(
            offset=offset,
            limit=page_size,
            include=["documents", "metadatas"],
        )
        ids = page.get("ids") or []
        docs = page.get("documents") or []
        metas = page.get("metadatas") or []
        for idx, row_id in enumerate(ids):
            doc = docs[idx] if idx < len(docs) else ""
            meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
            yield str(row_id), str(doc or ""), dict(meta)


def _load_irish_name_to_canonical() -> Dict[str, str]:
    cfg = _get_config()
    query = text(
        f"""
        SELECT "Food Name", "canonical_food_id"
        FROM "{cfg['schema']}"."{cfg['irish_ingredients_table']}"
        WHERE "canonical_food_id" IS NOT NULL
          AND "Food Name" IS NOT NULL
        ORDER BY row_id ASC
        """
    )
    out: Dict[str, str] = {}
    with get_connection() as conn:
        rows = conn.execute(query).fetchall()
    for food_name, canonical_food_id in rows:
        key = _norm_name(food_name)
        if key and key not in out:
            out[key] = str(canonical_food_id)
    return out


def _load_irish_name_to_canonical_from_csv(csv_path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            food_name = row.get("Food Name")
            canonical = row.get("canonical_food_id")
            key = _norm_name(food_name)
            if key and canonical and key not in out:
                out[key] = str(canonical).strip()
    return out


def _choose_name(meta: dict, document: str) -> Optional[str]:
    for candidate in (
        meta.get("Food Name"),
        meta.get("title"),
        document,
    ):
        key = _norm_name(candidate)
        if key:
            return key
    return None


def _flush_updates(collection, ids: List[str], metas: List[dict]) -> None:
    if not ids:
        return
    collection.update(ids=ids, metadatas=metas)


def run(
    collection_name: str,
    batch_size: int,
    dry_run: bool,
    source: str,
    csv_path: str,
) -> None:
    name_to_canonical: Dict[str, str] = {}
    source_mode = source.strip().lower()

    if source_mode in {"postgres", "auto"}:
        try:
            name_to_canonical = _load_irish_name_to_canonical()
            print(f"mapping_source=postgres entries={len(name_to_canonical)}")
        except Exception as exc:
            if source_mode == "postgres":
                raise
            print(f"mapping_source=postgres failed: {exc}")

    if not name_to_canonical and source_mode in {"csv", "auto"}:
        name_to_canonical = _load_irish_name_to_canonical_from_csv(csv_path)
        print(f"mapping_source=csv entries={len(name_to_canonical)} path={csv_path}")

    if not name_to_canonical:
        raise RuntimeError("No Irish canonical mappings found from configured sources.")

    client = get_chroma_client()
    collection = client.get_collection(collection_name)
    total = collection.count()

    updated = 0
    already_has_canonical = 0
    no_name = 0
    no_match = 0

    pending_ids: List[str] = []
    pending_metas: List[dict] = []

    for row_id, document, meta in _iter_collection_rows(collection):
        existing = str(meta.get("canonical_food_id") or "").strip()
        if existing:
            already_has_canonical += 1
            continue

        key = _choose_name(meta, document)
        if not key:
            no_name += 1
            continue

        canonical = name_to_canonical.get(key)
        if not canonical:
            no_match += 1
            continue

        meta["canonical_food_id"] = canonical
        if not meta.get("title"):
            meta["title"] = str(meta.get("Food Name") or document or "").strip()

        updated += 1
        pending_ids.append(row_id)
        pending_metas.append(meta)
        if len(pending_ids) >= batch_size:
            if not dry_run:
                _flush_updates(collection, pending_ids, pending_metas)
            pending_ids.clear()
            pending_metas.clear()

    if pending_ids and not dry_run:
        _flush_updates(collection, pending_ids, pending_metas)

    print(f"collection={collection_name}")
    print(f"total={total}")
    print(f"updated={updated}")
    print(f"already_has_canonical={already_has_canonical}")
    print(f"no_name={no_name}")
    print(f"no_match={no_match}")
    print(f"dry_run={dry_run}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--collection",
        default="nutritional_ingredients_irish",
        help="Target Chroma collection name.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Update batch size.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute stats without writing updates.",
    )
    parser.add_argument(
        "--source",
        default="auto",
        choices=["auto", "postgres", "csv"],
        help="Where to read Food Name -> canonical_food_id mappings from.",
    )
    parser.add_argument(
        "--csv-path",
        default="data/raw/irish/Irish_Comp_Table.csv",
        help="CSV path used when --source is csv or fallback in auto mode.",
    )
    args = parser.parse_args()

    if args.batch_size <= 0 or not math.isfinite(args.batch_size):
        raise ValueError("--batch-size must be a positive integer.")

    run(
        collection_name=args.collection,
        batch_size=int(args.batch_size),
        dry_run=bool(args.dry_run),
        source=str(args.source),
        csv_path=str(args.csv_path),
    )


if __name__ == "__main__":
    main()
