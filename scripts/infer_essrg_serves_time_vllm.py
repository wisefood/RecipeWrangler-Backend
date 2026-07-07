#!/usr/bin/env python3
"""Infer ESSRG serving count and total elapsed time through local vLLM.

The model returns exactly two values: ``serves`` and ``total_time_minutes``.
Results are append-only/resume-safe in JSONL and are merged atomically into the
golden ESSRG JSON. All added values are explicitly marked as LLM estimates.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai
from tqdm import tqdm


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data/ESSRG/ESSRG_recipes_clean.json"
DEFAULT_OUTPUT = REPO_ROOT / "data/ESSRG/ESSRG_serves_time_qwen14b.jsonl"
DEFAULT_MODEL = "qwen14b"

ESTIMATE_SCHEMA = {
    "name": "ESSRGServesAndTime",
    "schema": {
        "type": "object",
        "properties": {
            "serves": {"type": "integer", "minimum": 1, "maximum": 20},
            "total_time_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
            },
        },
        "required": ["serves", "total_time_minutes"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """Estimate exactly two missing recipe fields:
1. serves: how many people the complete meal quantities reasonably serve.
2. total_time_minutes: total elapsed time to prepare the complete meal.

Use only the supplied meal title, meal type, ingredient quantities, component
dishes, and instructions. The recipe can contain multiple component dishes.
For total time, account for tasks that can be performed in parallel; do not
simply add every component time. Do not invent ingredients or instructions.
Return only the two schema fields. Both values are estimates."""


def recipe_prompt(recipe: dict[str, Any]) -> str:
    ingredient_lines = []
    for item in recipe.get("ingredient_details") or []:
        measurement = item.get("measurement") or "quantity not specified"
        component = item.get("component") or "Meal"
        ingredient_lines.append(
            f"- [{component}] {measurement} {item.get('name', '')}".strip()
        )

    instruction_lines = [
        f"- {step}" for step in recipe.get("instructions") or []
    ]
    return "\n".join(
        [
            f"Meal: {recipe.get('title', '')}",
            f"Meal type: {recipe.get('meal_type') or 'unknown'}",
            f"Description: {recipe.get('description') or 'not provided'}",
            "",
            "Ingredients:",
            *(ingredient_lines or ["- none provided"]),
            "",
            "Component instructions:",
            *(instruction_lines or ["- none provided"]),
        ]
    )


def load_completed(path: Path) -> dict[str, dict[str, int]]:
    completed: dict[str, dict[str, int]] = {}
    if not path.exists():
        return completed
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") != "ok":
                continue
            recipe_id = str(row.get("recipe_id") or "")
            estimate = row.get("estimate") or {}
            if recipe_id and {"serves", "total_time_minutes"} <= estimate.keys():
                completed[recipe_id] = {
                    "serves": int(estimate["serves"]),
                    "total_time_minutes": int(estimate["total_time_minutes"]),
                }
    return completed


def apply_estimate(
    recipe: dict[str, Any],
    estimate: dict[str, int],
    model: str,
    estimated_at: str,
) -> None:
    serves = int(estimate["serves"])
    total_time = int(estimate["total_time_minutes"])

    recipe["serves"] = serves
    recipe["duration"] = total_time
    recipe["duration_minutes"] = total_time
    recipe["serves_source"] = "llm_estimate"
    recipe["duration_source"] = "llm_estimate"
    recipe["llm_estimation"] = {
        "model": model,
        "fields": ["serves", "total_time_minutes"],
        "estimated_at": estimated_at,
        "source_values_provided": False,
    }

    nutrition = recipe.get("nutrition")
    if isinstance(nutrition, dict):
        nutrition["serves"] = serves
        for key in (
            "energy_kcal",
            "protein_g",
            "carbohydrate_g",
            "fat_g",
            "sugar_g",
            "saturated_fat_g",
            "sodium_mg",
            "fibre_g",
        ):
            total = nutrition.get(key)
            nutrition[f"{key}_per_serving"] = (
                round(float(total) / serves, 6) if total is not None else None
            )
        nutrition["per_serving_basis"] = "llm_estimated_serves"


def atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default="http://127.0.0.1:8005/v1")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key", default="local-vllm")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    recipes = json.loads(args.input.read_text(encoding="utf-8"))
    completed = load_completed(args.output)
    client = openai.OpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)

    for recipe in tqdm(
        recipes,
        total=len(recipes),
        desc="ESSRG serves/time",
        unit="recipe",
        dynamic_ncols=True,
    ):
        recipe_id = str(recipe["recipe_id"])
        estimate = completed.get(recipe_id)
        estimated_at = datetime.now(timezone.utc).isoformat()

        if estimate is None:
            try:
                response = client.chat.completions.create(
                    model=args.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": recipe_prompt(recipe)},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": ESTIMATE_SCHEMA,
                    },
                    temperature=0.0,
                    max_tokens=64,
                )
                estimate = json.loads(response.choices[0].message.content or "{}")
                estimate = {
                    "serves": int(estimate["serves"]),
                    "total_time_minutes": int(estimate["total_time_minutes"]),
                }
                row = {
                    "recipe_id": recipe_id,
                    "status": "ok",
                    "estimate": estimate,
                    "model": args.model,
                    "estimated_at": estimated_at,
                }
                completed[recipe_id] = estimate
            except Exception as exc:
                row = {
                    "recipe_id": recipe_id,
                    "status": "error",
                    "error": str(exc),
                    "model": args.model,
                    "estimated_at": estimated_at,
                }

            with args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()

        if estimate is not None:
            apply_estimate(recipe, estimate, args.model, estimated_at)

        # Keep the golden JSON usable if the job is interrupted.
        atomic_write_json(args.input, recipes)

    successful = sum(
        1 for recipe in recipes if recipe.get("serves_source") == "llm_estimate"
    )
    print(f"Completed estimates: {successful}/{len(recipes)}")
    print(f"Golden JSON updated: {args.input}")
    print(f"Resume log: {args.output}")


if __name__ == "__main__":
    main()
