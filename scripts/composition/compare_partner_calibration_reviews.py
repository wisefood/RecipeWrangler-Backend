#!/usr/bin/env python3
"""Combine completed partner workbooks into an adjudication worklist."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from openpyxl import load_workbook


EXPERT_FIELDS = (
    "expert_facet_corrections",
    "expert_foodon_decision",
    "expert_foodon_id",
    "expert_foodon_acceptable_ids",
    "expert_composition_decision",
    "expert_composition_source",
    "expert_composition_record_id",
    "expert_composition_acceptable_records",
    "expert_abstention_is_valid",
    "expert_choice_or_compound_decision",
    "expert_rationale",
)
REQUIRED_FIELDS = (
    "expert_foodon_decision",
    "expert_composition_decision",
    "expert_choice_or_compound_decision",
    "reviewer",
    "reviewed_at",
)


def _read(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    sheet = load_workbook(path, read_only=True, data_only=True)["Review"]
    values = sheet.iter_rows(values_only=True)
    headers = [str(value or "") for value in next(values)]
    return [dict(zip(headers, ("" if value is None else str(value) for value in row))) for row in values]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reviews", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.reviews) < 2:
        parser.error("at least two independent review files are required")

    reviews = [_read(path) for path in args.reviews]
    item_ids = [row["item_id"] for row in reviews[0]]
    if len(item_ids) != 30 or len(set(item_ids)) != 30:
        raise ValueError("The first review must contain 30 unique item IDs")
    indexed = [{row["item_id"]: row for row in review} for review in reviews]
    if any(set(review) != set(item_ids) for review in indexed):
        raise ValueError("All reviews must contain the same item IDs")
    for path, review in zip(args.reviews, reviews):
        missing = [
            f"{row.get('item_id', '?')}:{field}"
            for row in review
            for field in REQUIRED_FIELDS
            if not row.get(field, "").strip()
        ]
        if missing:
            raise ValueError(f"Incomplete review {path}: {', '.join(missing[:5])}")

    output_rows = []
    for item_id in item_ids:
        rows = [review[item_id] for review in indexed]
        output = {
            "item_id": item_id,
            "name": rows[0]["name"],
            "display": rows[0]["display"],
        }
        disagreements = []
        for field in EXPERT_FIELDS:
            values = [row.get(field, "").strip() for row in rows]
            output[f"reviewers_{field}"] = " || ".join(values)
            if len(set(values)) > 1:
                disagreements.append(field)
        output["agreement"] = "yes" if not disagreements else "no"
        output["disagreement_fields"] = ";".join(disagreements)
        output["adjudicated_foodon_decision"] = ""
        output["adjudicated_foodon_ids"] = ""
        output["adjudicated_composition_decision"] = ""
        output["adjudicated_composition_records"] = ""
        output["adjudicated_facets"] = ""
        output["adjudicated_choice_or_compound"] = ""
        output["adjudication_rationale"] = ""
        output_rows.append(output)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
