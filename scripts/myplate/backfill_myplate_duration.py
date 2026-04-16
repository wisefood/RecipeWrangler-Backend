#!/usr/bin/env python3
"""Backfill missing duration on MyPlate recipes.

Strategy (in order):
  1. Title heuristic   – "20-Minute Chicken Creole" → 20
  2. Regex extraction  – sum all explicit time mentions in instruction text
  3. Groq LLM fallback – estimate from title + ingredients + instructions

Writes to:
  - data/MyPlate/myplate_recipes_clean.json   (source JSON)
  - Neo4j Recipe nodes  (SET r.duration)
  - PostgreSQL trace column is NOT touched (the nutrition values are unaffected)

Usage:
    # Dry-run (prints what would be set):
    python3 scripts/myplate/backfill_myplate_duration.py

    # Write everywhere:
    python3 scripts/myplate/backfill_myplate_duration.py --write

    # Limit LLM calls for testing:
    python3 scripts/myplate/backfill_myplate_duration.py --write --llm-limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_API_KEY"] = ""
os.environ["LANGSMITH_API_KEY"] = ""

from groq import Groq  # noqa: E402
from tqdm import tqdm  # noqa: E402

from recipe_wrangler.utils.neo4j_utils import run_query  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "data" / "MyPlate" / "myplate_recipes_clean.json"
GROQ_MODEL = "llama-3.1-8b-instant"
LLM_BATCH_SIZE = 10
LLM_RATE_LIMIT_SLEEP = 2.0  # seconds between batches (stay under 30 req/min)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Written-out numbers for common cooking times
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
    "sixty": 60,
}
_WORD_NUM_PATTERN = "|".join(re.escape(w) for w in _WORD_NUMBERS)

# Match "10 to 15 minutes", "10-15 minutes", "10 minutes", "1 hour", "ten minutes"
_TIME_RE = re.compile(
    rf"(?:(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)|({_WORD_NUM_PATTERN}))"
    rf"\s*(?:to\s+\d+\s*)?"
    rf"(hours?|hr\.?s?|minutes?|mins?)",
    re.IGNORECASE,
)

# Title pattern: "20-Minute ...", "30 Minute ...", "1-Hour ..."
_TITLE_RE = re.compile(
    r"\b(\d+)\s*[-\s](?:minute|min|hour|hr)s?\b",
    re.IGNORECASE,
)


def _extract_duration_regex(title: str, instructions: list[str]) -> float | None:
    """Sum all time mentions in instructions; fallback to title heuristic."""
    text = " ".join(instructions or [])
    total_minutes = 0.0
    found_any = False

    for m in _TIME_RE.finditer(text):
        range_lo, range_hi, single, word, unit = m.groups()
        unit_l = unit.lower()

        if range_lo and range_hi:
            value = (float(range_lo) + float(range_hi)) / 2
        elif single:
            value = float(single)
        elif word:
            value = float(_WORD_NUMBERS.get(word.lower(), 0))
        else:
            continue

        if "hour" in unit_l or unit_l in ("hr", "hr.", "hrs", "hrs."):
            value *= 60

        # Filter out implausible values (oven temps disguised as times, etc.)
        if value <= 0 or value > 480:
            continue

        total_minutes += value
        found_any = True

    if found_any and total_minutes > 0:
        return round(total_minutes)

    # Title heuristic
    m = _TITLE_RE.search(title or "")
    if m:
        val = float(m.group(1))
        if "hour" in m.group(0).lower() or "hr" in m.group(0).lower():
            val *= 60
        return round(val)

    return None


# ---------------------------------------------------------------------------
# LLM batch estimation
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a cooking time estimator. "
    "For each recipe, output ONLY a JSON object mapping recipe_id to total cooking "
    "time in minutes (integer). No explanation, no markdown, just raw JSON. "
    "Example: {\"abc123\": 35, \"def456\": 20}"
)


def _build_user_prompt(batch: list[dict[str, Any]]) -> str:
    lines = []
    for r in batch:
        title = r["title"]
        ingredients = ", ".join(r.get("ingredients") or [])
        instructions = " ".join(r.get("instructions") or [])[:400]
        lines.append(
            f'recipe_id: {r["recipe_id"]}\n'
            f'title: {title}\n'
            f'ingredients: {ingredients}\n'
            f'instructions: {instructions}'
        )
    return "\n\n---\n\n".join(lines)


def _call_groq_batch(client: Groq, batch: list[dict[str, Any]]) -> dict[str, int]:
    """Call Groq for a batch of recipes, return {recipe_id: minutes}."""
    prompt = _build_user_prompt(batch)
    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.0,
            max_tokens=128,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)
        return {str(k): int(v) for k, v in result.items() if v and int(v) > 0}
    except Exception as exc:
        tqdm.write(f"  [LLM error] {exc} — skipping batch")
        return {}


# ---------------------------------------------------------------------------
# Neo4j update
# ---------------------------------------------------------------------------

def _update_neo4j(updates: list[dict[str, Any]], dry_run: bool) -> int:
    if dry_run or not updates:
        return 0
    result = run_query(
        """
        UNWIND $updates AS u
        MATCH (r:Recipe {source: 'MyPlate', recipe_id: u.recipe_id})
        SET r.duration = u.duration
        RETURN count(r) AS n
        """,
        {"updates": updates},
    )
    return result[0]["n"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Persist to JSON + Neo4j.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--llm-limit", type=int, default=None,
                        help="Cap LLM calls to this many recipes (for testing).")
    args = parser.parse_args()

    raw: dict[str, Any] = json.loads(args.input.read_text(encoding="utf-8"))
    recipes = list(raw.values())

    # Only process recipes with missing or zero duration
    needs_duration = [
        r for r in recipes
        if isinstance(r, dict) and not (r.get("duration") and float(r.get("duration") or 0) > 0)
    ]
    print(f"Recipes needing duration: {len(needs_duration)} / {len(recipes)}")

    resolved_regex = 0
    resolved_llm = 0
    unresolved = 0
    llm_pending: list[dict[str, Any]] = []

    # Pass 1: regex
    for r in needs_duration:
        duration = _extract_duration_regex(
            r.get("title", ""),
            r.get("instructions") or [],
        )
        if duration:
            r["_resolved_duration"] = duration
            r["_resolved_by"] = "regex"
            resolved_regex += 1
        else:
            llm_pending.append(r)

    print(f"  Resolved by regex/title: {resolved_regex}")
    print(f"  Needs LLM:               {len(llm_pending)}")

    # Pass 2: LLM
    if llm_pending:
        if args.llm_limit:
            llm_pending = llm_pending[: args.llm_limit]
            print(f"  (LLM limited to {args.llm_limit} recipes)")

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            print("WARNING: GROQ_API_KEY not set — skipping LLM pass.")
        else:
            client = Groq(api_key=api_key)
            batches = [
                llm_pending[i: i + LLM_BATCH_SIZE]
                for i in range(0, len(llm_pending), LLM_BATCH_SIZE)
            ]
            bar = tqdm(batches, desc="LLM estimation", unit="batch")
            for batch in bar:
                results = _call_groq_batch(client, batch)
                for r in batch:
                    rid = str(r.get("recipe_id") or r.get("id") or "")
                    if rid in results and results[rid] > 0:
                        r["_resolved_duration"] = results[rid]
                        r["_resolved_by"] = "llm"
                        resolved_llm += 1
                    else:
                        unresolved += 1
                bar.set_postfix(llm=resolved_llm, unresolved=unresolved)
                time.sleep(LLM_RATE_LIMIT_SLEEP)

    # Collect all resolutions
    neo4j_updates: list[dict[str, Any]] = []
    for r in recipes:
        if isinstance(r, dict) and "_resolved_duration" in r:
            duration = r["_resolved_duration"]
            recipe_id = str(r.get("recipe_id") or r.get("id") or "")
            # Update in-memory JSON
            r["duration"] = duration
            # Queue Neo4j update
            if recipe_id:
                neo4j_updates.append({"recipe_id": recipe_id, "duration": duration})
            # Clean up temp keys
            del r["_resolved_duration"]
            del r["_resolved_by"]

    print(f"\nResolution summary:")
    print(f"  regex/title : {resolved_regex}")
    print(f"  llm         : {resolved_llm}")
    print(f"  unresolved  : {unresolved}")
    print(f"  neo4j queue : {len(neo4j_updates)}")

    if args.write:
        # Write updated JSON
        args.input.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nWrote updated JSON → {args.input}")

        # Update Neo4j
        updated = _update_neo4j(neo4j_updates, dry_run=False)
        print(f"Neo4j nodes updated: {updated}")
    else:
        print("\nDry-run — no writes. Pass --write to persist.")
        # Show a sample
        samples = [r for r in recipes if isinstance(r, dict) and r.get("duration") and float(r.get("duration") or 0) > 0][:10]
        print("\nSample resolved durations:")
        for r in samples:
            print(f"  {r.get('title','?'):50s} → {r.get('duration')} min")


if __name__ == "__main__":
    main()
