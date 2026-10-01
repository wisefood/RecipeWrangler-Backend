"""Promote only independently verified OpenRouter weight candidates."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.run_openrouter_weight_review import (
        DEFAULT_AUDIT,
        DEFAULT_OUTPUT as DEFAULT_LLM_OUTPUT,
        _add_source_context,
        _load_signatures,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from run_openrouter_weight_review import (
        DEFAULT_AUDIT,
        DEFAULT_OUTPUT as DEFAULT_LLM_OUTPUT,
        _add_source_context,
        _load_signatures,
    )
from recipe_wrangler.tools.ingredient_weight_tool import (
    _VOLUME_UNIT_ML,
    _clean_unit,
    _common_unit_reference_grams,
    _liquid_density_for_name,
    _parse_quantity_value,
    _split_measurement,
)


SNAPSHOT_ROOT = DEFAULT_AUDIT.parents[1]
DEFAULT_REVIEW = SNAPSHOT_ROOT / "weight/llm/gpt_oss_120b_semantic_review.json"
DEFAULT_ACCEPTED = SNAPSHOT_ROOT / "weight/reviewed_openrouter_weight_references.csv"

_MASS_RE = re.compile(
    r"(?i)(\d+(?:\.\d+)?)\s*(kg|kilograms?|g|grams?|oz|ounces?|lb|lbs|pounds?)\b"
)
_MASS_FACTORS = {
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= max(0.5, max(left, right) * 0.05)


def _source_mass_per_original_unit(
    signature: dict[str, Any], total_grams: float, quantity: float
) -> float | None:
    # Source evidence is recipe-specific. Never let one example's note become
    # a signature-wide reference shared by other recipes.
    examples = signature.get("examples") or []
    if int(signature.get("occurrences") or 0) != 1 or len(examples) != 1:
        return None
    context = " ".join(
        str(examples[0].get(key) or "") for key in ("display", "note")
    )
    for amount, unit in _MASS_RE.findall(context):
        source_grams = float(amount) * _MASS_FACTORS[unit.lower()]
        if _close(source_grams, total_grams):
            return source_grams / quantity
        if re.search(r"(?i)\beach\b", context) and _close(
            source_grams, total_grams / quantity
        ):
            return source_grams
    return None


def _independent_reference(
    signature: dict[str, Any], candidate: dict[str, Any]
) -> tuple[float, str] | None:
    result = candidate.get("result") or {}
    if candidate.get("validation_status") != "valid_pending_review":
        return None
    try:
        total_grams = float(result["total_grams"])
    except (KeyError, TypeError, ValueError):
        return None

    quantity_text, unit_text, _ = _split_measurement(signature.get("measurement"))
    quantity = _parse_quantity_value(quantity_text)
    unit = _clean_unit(str(unit_text or ""))
    if quantity is None or quantity <= 0 or not unit:
        return None
    candidate_grams_per_unit = total_grams / quantity

    source_grams = _source_mass_per_original_unit(
        signature, total_grams, quantity
    )
    if source_grams is not None:
        return source_grams, "source_explicit_mass"

    common = _common_unit_reference_grams(
        signature["ingredient"], unit, measurement=signature["measurement"]
    )
    if common is not None and _close(float(common[0]), candidate_grams_per_unit):
        return float(common[0]), f"curated_portion:{common[1]}"

    density = _liquid_density_for_name(signature["ingredient"])
    if density is not None and unit in _VOLUME_UNIT_ML:
        expected = float(density[0]) * _VOLUME_UNIT_ML[unit]
        if _close(expected, candidate_grams_per_unit):
            return expected, f"curated_density:{density[1]}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--llm-output", type=Path, default=DEFAULT_LLM_OUTPUT)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--accepted-output", type=Path, default=DEFAULT_ACCEPTED)
    args = parser.parse_args()

    signatures = _load_signatures(args.audit)
    _add_source_context(signatures, args.audit)
    llm_rows = {
        row["signature_id"]: row
        for row in (
            json.loads(line)
            for line in args.llm_output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }

    reviewed = []
    accepted_rows = []
    for signature in signatures:
        candidate = llm_rows.get(signature["signature_id"])
        if candidate is None:
            status, reason = "unresolved", "no_llm_candidate_for_current_signature"
            reference = None
        else:
            reference = _independent_reference(signature, candidate)
            if reference is not None:
                status, reason = "accepted_deterministic", reference[1]
            elif candidate.get("validation_status") == "abstained":
                status, reason = "unresolved", "model_abstained"
            elif candidate.get("validation_status") == "invalid":
                status, reason = "unresolved", candidate.get("validation_reason")
            else:
                status, reason = "rejected_unverified", "no_independent_evidence"

        reviewed.append(
            {
                **signature,
                "review_status": status,
                "review_reason": reason,
                "nutrition_identity_pending": signature["error"] == "missing_usda_id",
                "candidate": None if candidate is None else candidate.get("result"),
            }
        )
        if reference is None:
            continue
        _, unit_text, _ = _split_measurement(signature["measurement"])
        unit = _clean_unit(str(unit_text or ""))
        accepted_rows.append(
            {
                "ingredient": signature["ingredient"],
                "normalized_unit": unit,
                "reference_measurement": f"1 {unit}",
                "weight_grams": round(reference[0], 6),
                "source_type": "accepted_deterministic",
                "confidence": 0.9,
                "reason_tag": reference[1],
                "signature_id": signature["signature_id"],
                "occurrences": signature["occurrences"],
            }
        )

    counts = Counter(row["review_status"] for row in reviewed)
    uses = Counter()
    for row in reviewed:
        uses[row["review_status"]] += int(row["occurrences"])
    report = {
        "audit": str(args.audit),
        "llm_output": str(args.llm_output),
        "summary": {"signatures": dict(counts), "ingredient_uses": dict(uses)},
        "rows": reviewed,
    }
    if args.accepted_output.exists():
        with args.accepted_output.open("r", encoding="utf-8-sig", newline="") as handle:
            accepted_rows.extend(csv.DictReader(handle))
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in accepted_rows:
        key = (row["ingredient"].casefold(), row["normalized_unit"])
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = row
        elif _close(float(existing["weight_grams"]), float(row["weight_grams"])):
            existing["occurrences"] = max(
                int(existing["occurrences"]), int(row["occurrences"])
            )
    accepted_rows = list(deduplicated.values())
    report["summary"]["accepted_reference_rows"] = len(accepted_rows)
    args.review_output.parent.mkdir(parents=True, exist_ok=True)
    args.review_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with args.accepted_output.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "ingredient", "normalized_unit", "reference_measurement",
            "weight_grams", "source_type", "confidence", "reason_tag",
            "signature_id", "occurrences",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(accepted_rows)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
