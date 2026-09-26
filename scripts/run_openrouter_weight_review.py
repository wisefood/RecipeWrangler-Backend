"""Estimate unresolved ingredient weights with OpenRouter, pending review only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from recipe_wrangler.utils.env_loader import load_runtime_env


REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "data/processed/ingredient_parsing_final/2026-09-24"
DEFAULT_AUDIT = SNAPSHOT_ROOT / "weight/final_deterministic_weight_audit.json"
DEFAULT_OUTPUT = SNAPSHOT_ROOT / "weight/llm/gpt_oss_120b_pending_review.jsonl"
MODEL = "openai/gpt-oss-120b:exacto"
PROMPT_VERSION = "weight-review-v1"

SYSTEM_PROMPT = """You estimate cooking-ingredient weight in grams from source context.
Return an estimate only when quantity and unit/portion can be recovered, or a conventional
portion has a defensible typical weight. Never invent a missing quantity for a nutritionally
significant ingredient. If evidence is insufficient, abstain and set all estimate numbers to
null. total_grams must equal quantity * grams_per_unit. The plausible range must contain the
total. Prefer abstention over a confident guess. Keep rationale under 30 words. Return only
the required JSON schema."""

RESULT_PROPERTIES = {
    "signature_id": {"type": "string"},
    "decision": {"type": "string", "enum": ["estimate", "abstain"]},
    "normalized_unit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "quantity": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "grams_per_unit": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "total_grams": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "min_total_grams": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "max_total_grams": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "confidence": {"type": "number"},
    "reason_code": {
        "type": "string",
        "enum": [
            "source_explicit",
            "common_reference",
            "context_inference",
            "missing_information",
            "ambiguous_portion",
            "ambiguous_identity",
        ],
    },
    "rationale": {"type": "string"},
}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "weight_estimates",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": RESULT_PROPERTIES,
                        "required": list(RESULT_PROPERTIES),
                    },
                }
            },
            "required": ["results"],
        },
    },
}


def _signature_id(ingredient: str, measurement: str, error: str) -> str:
    raw = json.dumps(
        [ingredient.casefold().strip(), measurement.strip(), error.strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _load_signatures(audit_path: Path) -> list[dict[str, Any]]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    grouped: dict[str, dict[str, Any]] = {}
    for row in audit["high_risk_rows"]:
        if float(row.get("weight_g") or 0) != 0:
            continue
        ingredient = str(row.get("ingredient") or "").strip()
        measurement = str(row.get("measurement") or "").strip()
        error = str(row.get("error") or "").strip()
        signature_id = _signature_id(ingredient, measurement, error)
        item = grouped.setdefault(
            signature_id,
            {
                "signature_id": signature_id,
                "ingredient": ingredient,
                "measurement": measurement,
                "error": error,
                "occurrences": 0,
                "examples": [],
            },
        )
        item["occurrences"] += 1
        example = {
            "source_file": row.get("source_file"),
            "recipe_title": row.get("recipe_title"),
        }
        if example not in item["examples"] and len(item["examples"]) < 3:
            item["examples"].append(example)
    return sorted(grouped.values(), key=lambda row: (-row["occurrences"], row["signature_id"]))


def _add_source_context(signatures: list[dict[str, Any]], audit_path: Path) -> None:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    lookup: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for raw_path in audit["corpus"]["recipe_files"]:
        path = Path(raw_path)
        for recipe in json.loads(path.read_text(encoding="utf-8")):
            title = str(recipe.get("title") or "")
            for ingredient in recipe.get("ingredients") or []:
                key = (
                    path.name,
                    title,
                    str(ingredient.get("name") or "").casefold().strip(),
                    str(ingredient.get("measurement") or "").strip(),
                )
                lookup.setdefault(key, []).append(
                    {
                        "display": str(ingredient.get("display") or ""),
                        "note": str(ingredient.get("note") or ""),
                    }
                )
    for signature in signatures:
        for example in signature["examples"]:
            key = (
                str(example["source_file"]),
                str(example["recipe_title"]),
                signature["ingredient"].casefold(),
                signature["measurement"],
            )
            context = (lookup.get(key) or [{}])[0]
            example.update(context)


def _validate(result: dict[str, Any]) -> tuple[str, str]:
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return "invalid", "confidence_out_of_range"
    numeric_keys = (
        "quantity",
        "grams_per_unit",
        "total_grams",
        "min_total_grams",
        "max_total_grams",
    )
    if result.get("decision") == "abstain":
        if any(result.get(key) is not None for key in numeric_keys):
            return "invalid", "abstention_contains_estimate"
        return "abstained", "model_abstained"
    try:
        quantity, grams_per_unit, total, low, high = (
            float(result[key]) for key in numeric_keys
        )
    except (KeyError, TypeError, ValueError):
        return "invalid", "missing_numeric_estimate"
    if min(quantity, grams_per_unit, total, low, high) <= 0 or total > 100_000:
        return "invalid", "implausible_nonpositive_or_huge_value"
    if not low <= total <= high:
        return "invalid", "range_does_not_contain_total"
    expected = quantity * grams_per_unit
    if abs(expected - total) > max(0.1, total * 0.02):
        return "invalid", "arithmetic_mismatch"
    return "valid_pending_review", "ok"


def _completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            completed.add(json.loads(line)["signature_id"])
        except (KeyError, json.JSONDecodeError):
            continue
    return completed


def _request_results(
    client: Any, model: str, chunk: list[dict[str, Any]], batch_label: int
) -> tuple[dict[str, dict[str, Any]], int, int]:
    input_tokens = output_tokens = 0
    expected_ids = {row["signature_id"] for row in chunk}
    last_error: Exception | None = None
    for attempt in range(3):
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
            ],
            response_format=RESPONSE_FORMAT,
            extra_body={"reasoning": {"effort": "low"}},
        )
        usage = response.usage.model_dump() if response.usage else {}
        input_tokens += int(usage.get("prompt_tokens") or 0)
        output_tokens += int(usage.get("completion_tokens") or 0)
        try:
            payload = json.loads(response.choices[0].message.content or "{}")
            payload_results = (
                payload if isinstance(payload, list) else payload.get("results", [])
            )
            results = {row["signature_id"]: row for row in payload_results}
            if set(results) != expected_ids:
                raise ValueError(
                    "model returned missing, extra, or duplicate signature IDs"
                )
            return results, input_tokens, output_tokens
        except (AttributeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                print(json.dumps({"retrying_batch": batch_label, "attempt": attempt + 2}))

    if len(chunk) == 1:
        raise last_error or RuntimeError("model response validation failed")

    print(json.dumps({"splitting_batch": batch_label, "items": len(chunk)}))
    merged: dict[str, dict[str, Any]] = {}
    for item in chunk:
        item_results, item_input, item_output = _request_results(
            client, model, [item], batch_label
        )
        merged.update(item_results)
        input_tokens += item_input
        output_tokens += item_output
    return merged, input_tokens, output_tokens


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()

    load_runtime_env()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    signatures = _load_signatures(args.audit)
    _add_source_context(signatures, args.audit)
    completed = _completed_ids(args.output)
    pending = [row for row in signatures if row["signature_id"] not in completed]
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print(json.dumps({"status": "complete", "completed": len(completed)}))
        return 0

    client = OpenAI(
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=api_key,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    processed = estimated = abstained = invalid = input_tokens = output_tokens = 0
    with args.output.open("a", encoding="utf-8") as handle:
        for offset in range(0, len(pending), args.batch_size):
            chunk = pending[offset : offset + args.batch_size]
            results, batch_input, batch_output = _request_results(
                client, args.model, chunk, offset
            )
            input_tokens += batch_input
            output_tokens += batch_output
            for signature in chunk:
                result = results[signature["signature_id"]]
                validation_status, validation_reason = _validate(result)
                estimated += validation_status == "valid_pending_review"
                abstained += validation_status == "abstained"
                invalid += validation_status == "invalid"
                record = {
                    **signature,
                    "review_status": "pending_review",
                    "model": args.model,
                    "prompt_version": PROMPT_VERSION,
                    "result": result,
                    "validation_status": validation_status,
                    "validation_reason": validation_reason,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                processed += 1
            handle.flush()
            print(json.dumps({"processed": processed, "remaining": len(pending) - processed}))

    print(
        json.dumps(
            {
                "processed": processed,
                "valid_pending_review": estimated,
                "abstained": abstained,
                "invalid": invalid,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_openrouter_cost_usd": round(
                    input_tokens / 1_000_000 * 0.03
                    + output_tokens / 1_000_000 * 0.17,
                    6,
                ),
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
