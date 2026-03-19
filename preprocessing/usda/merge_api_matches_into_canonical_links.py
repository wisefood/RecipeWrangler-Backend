#!/usr/bin/env python3
"""Merge USDA API ingredient matches into canonical->USDA mapping JSON."""

from __future__ import annotations

import argparse
import csv
import json
import uuid
from pathlib import Path

from recipe_wrangler.utils.usda_nutrients_v1 import _normalize_canonical_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--api-matches-csv",
        type=Path,
        default=Path("notebooks/usda_ingredients.csv"),
        help="CSV with columns: Original Name, USDA Description, FDC ID, URL",
    )
    p.add_argument(
        "--sr-legacy-map-csv",
        type=Path,
        default=Path("data/raw/usda/sr_legacy_food.csv"),
        help="CSV with columns: fdc_id, NDB_number",
    )
    p.add_argument(
        "--food-des-path",
        type=Path,
        default=Path("data/raw/usda/FOOD_DES.txt"),
        help="USDA FOOD_DES.txt for food label + food group id.",
    )
    p.add_argument(
        "--fd-group-path",
        type=Path,
        default=Path("data/raw/usda/FD_GROUP.txt"),
        help="USDA FD_GROUP.txt for food group labels.",
    )
    p.add_argument(
        "--links-json",
        type=Path,
        default=Path("data/mappings/recipe1m-usda-links-canonical.json"),
        help="Canonical->USDA links JSON to update.",
    )
    p.add_argument(
        "--namespace",
        default="recipe-wrangler/usda-api-manual",
        help="UUID namespace seed for deterministic canonical_id generation.",
    )
    return p.parse_args()


def _unquote_tilde(value: str) -> str:
    return str(value or "").strip().strip("~").strip('"').strip()


def load_sr_legacy_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        low = {k.lower(): k for k in (reader.fieldnames or [])}
        fdc_col = low.get("fdc_id")
        ndb_col = low.get("ndb_number")
        if not fdc_col or not ndb_col:
            raise ValueError(f"Missing expected columns in {path}")
        for row in reader:
            fdc = str(row.get(fdc_col) or "").strip()
            ndb = str(row.get(ndb_col) or "").strip()
            if fdc and ndb:
                out[fdc] = ndb
    return out


def load_fd_groups(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with path.open("r", encoding="latin-1") as f:
        for line in f:
            parts = [p.strip() for p in line.rstrip("\n").split("^")]
            if len(parts) < 2:
                continue
            group_id = _unquote_tilde(parts[0])
            group_name = _unquote_tilde(parts[1])
            if group_id:
                out[group_id] = group_name
    return out


def load_food_des(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="latin-1") as f:
        for line in f:
            parts = [p.strip() for p in line.rstrip("\n").split("^")]
            if len(parts) < 3:
                continue
            usda_id = _unquote_tilde(parts[0])
            food_group_id = _unquote_tilde(parts[1])
            food_label = _unquote_tilde(parts[2])
            if usda_id:
                out[usda_id] = {
                    "usda_food_label": food_label,
                    "food_group_id": food_group_id,
                }
    return out


def main() -> None:
    args = parse_args()

    links = json.loads(args.links_json.read_text(encoding="utf-8"))
    if not isinstance(links, list):
        raise ValueError(f"Expected list JSON at {args.links_json}")

    sr_map = load_sr_legacy_map(args.sr_legacy_map_csv)
    food_des = load_food_des(args.food_des_path)
    fd_groups = load_fd_groups(args.fd_group_path)

    existing_norm_names = {
        _normalize_canonical_name(str(row.get("canonical") or ""))
        for row in links
        if row.get("canonical")
    }
    # Useful fallback for labels/groups if FOOD_DES doesn't have an id.
    by_usda_id = {}
    for row in links:
        usda_id = str(row.get("usda_id") or "").strip()
        if usda_id and usda_id not in by_usda_id:
            by_usda_id[usda_id] = row

    ns = uuid.uuid5(uuid.NAMESPACE_DNS, args.namespace)

    added = 0
    skipped_existing = 0
    skipped_no_sr = 0
    skipped_bad = 0

    with args.api_matches_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = str(row.get("Original Name") or "").strip()
            fdc_id = str(row.get("FDC ID") or "").strip()
            if not name or not fdc_id or fdc_id.upper() == "N/A":
                skipped_bad += 1
                continue

            norm_name = _normalize_canonical_name(name)
            if not norm_name:
                skipped_bad += 1
                continue
            if norm_name in existing_norm_names:
                skipped_existing += 1
                continue

            ndb = sr_map.get(fdc_id)
            if not ndb:
                skipped_no_sr += 1
                continue

            usda_id = str(ndb).zfill(5)
            meta = food_des.get(usda_id, {})
            fallback = by_usda_id.get(usda_id, {})
            food_group_id = str(
                meta.get("food_group_id")
                or fallback.get("food_group_id")
                or ""
            ).strip()
            food_group = str(
                fd_groups.get(food_group_id)
                or fallback.get("food_group")
                or ""
            ).strip()
            usda_label = str(
                meta.get("usda_food_label")
                or fallback.get("usda_food_label")
                or row.get("USDA Description")
                or ""
            ).strip()

            links.append(
                {
                    "canonical_id": str(uuid.uuid5(ns, norm_name)),
                    "canonical": name,
                    "usda_id": usda_id,
                    "usda_food_label": usda_label,
                    "food_group_id": food_group_id,
                    "food_group": food_group,
                    "embedding_similarity_source": "usda_api_sr_legacy",
                }
            )
            existing_norm_names.add(norm_name)
            added += 1

    links.sort(key=lambda x: str(x.get("canonical") or "").lower())
    args.links_json.write_text(
        json.dumps(links, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"added={added}")
    print(f"skipped_existing={skipped_existing}")
    print(f"skipped_no_sr={skipped_no_sr}")
    print(f"skipped_bad={skipped_bad}")
    print(f"total_links={len(links)}")
    print(f"updated={args.links_json}")


if __name__ == "__main__":
    main()
