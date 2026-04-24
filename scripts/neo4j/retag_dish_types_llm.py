#!/usr/bin/env python3
"""Re-classify dish-type tags using a Groq LLM.

For every recipe that already has a dish-type Tag in Neo4j, send title +
ingredients to the LLM and ask which meal slots (breakfast / lunch / dinner /
snack / dessert) the dish actually belongs to.  Then:
  - Remove the old dish-type tag(s) from the recipe
  - Write the corrected tag(s) back with category = 'dish-type'

Optionally syncs the same correction to ElasticSearch.

Usage
-----
  # Dry run (no writes) — shows what would change
  python scripts/neo4j/retag_dish_types_llm.py

  # Actually write to Neo4j
  python scripts/neo4j/retag_dish_types_llm.py --write

  # Write Neo4j + ES
  python scripts/neo4j/retag_dish_types_llm.py --write --sync-es

  # Limit to a specific dish-type slot (e.g. only re-check breakfast)
  python scripts/neo4j/retag_dish_types_llm.py --write --slot breakfast

  # Batch size / concurrency
  python scripts/neo4j/retag_dish_types_llm.py --write --batch 50 --workers 8
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()

from recipe_wrangler.utils.neo4j_utils import run_query

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SLOTS = {"breakfast", "main-dish", "side-dish", "snacks", "desserts", "beverages"}

# Map the LLM's plain-English answers to the canonical Neo4j tag names
LLM_TO_SLOT: dict[str, str] = {
    "breakfast": "breakfast",
    "lunch": "main-dish",
    "dinner": "main-dish",
    "main-dish": "main-dish",
    "main dish": "main-dish",
    "side": "side-dish",
    "side-dish": "side-dish",
    "side dish": "side-dish",
    "snack": "snacks",
    "snacks": "snacks",
    "dessert": "desserts",
    "desserts": "desserts",
    "beverage": "beverages",
    "beverages": "beverages",
    "drink": "beverages",
}

GROQ_MODEL = os.getenv("GROQ_RETAG_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = """\
Classify the dish into one or more meal slots.
Allowed values: breakfast, main-dish, side-dish, snacks, desserts, beverages.
Return a JSON object {"slots": [...]}.
Rules:
- breakfast = only genuine breakfast foods (pancakes, eggs, oatmeal, smoothies).
- side-dish = accompaniments that usually aren't eaten alone (plain vegetables, rice, bread, simple salads, sauces, dressings).
- main-dish = substantial entrees (stews, curries, pasta, roasts, casseroles, hearty bowls, stuffed dishes).
- Stews/curries/pasta/roasts = main-dish, not breakfast.
- A dish may have multiple slots when appropriate."""

SLOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["breakfast", "main-dish", "side-dish", "snacks", "desserts", "beverages"],
            },
            "minItems": 1,
        }
    },
    "required": ["slots"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

EXCLUDED_SOURCES = {"recipe1m"}


def fetch_recipes_with_dish_type(slot: str | None = None) -> list[dict]:
    """Return all recipes that have at least one dish-type tag.

    Recipes whose `source` (case-insensitive) is in EXCLUDED_SOURCES are skipped.
    If *slot* is given, restrict to recipes tagged with that specific slot.
    Returns list of dicts: {recipe_id, title, ingredients, current_slots}.
    """
    slot_filter = (
        f"AND toLower(dt.name) = '{slot}'" if slot else ""
    )
    query = f"""
    MATCH (r:Recipe)-[:HAS_TAG]->(dt:Tag)
    WHERE dt.category = 'dish-type'
    {slot_filter}
      AND r.title IS NOT NULL
      AND NOT toLower(coalesce(r.source, '')) IN $excluded_sources
    WITH r, collect(toLower(dt.name)) AS current_slots
    OPTIONAL MATCH (r)-[:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original)
    WITH r, current_slots, collect(o.name)[..20] AS ing_samples
    RETURN
      coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
      r.title  AS title,
      reduce(s = '', x IN ing_samples | CASE WHEN s = '' THEN x ELSE s + ', ' + x END)
        AS ingredients,
      current_slots
    """
    rows = run_query(query, {"excluded_sources": [s.lower() for s in EXCLUDED_SOURCES]})
    return [
        {
            "recipe_id": str(r["recipe_id"]).strip(),
            "title":     str(r["title"]).strip(),
            "ingredients": str(r.get("ingredients") or ""),
            "current_slots": list(r.get("current_slots") or []),
        }
        for r in rows
        if r.get("recipe_id") and r.get("title")
    ]


def write_dish_type_tags(recipe_id: str, new_slots: list[str]) -> None:
    """Replace all dish-type tags on a recipe with *new_slots*."""
    # 1. Remove existing dish-type tags
    run_query(
        """
        MATCH (r:Recipe)-[rel:HAS_TAG]->(dt:Tag)
        WHERE (r.recipe_id = $rid OR r.id = $rid)
          AND dt.category = 'dish-type'
        DELETE rel
        """,
        {"rid": recipe_id},
    )
    # 2. Write new ones
    for slot in new_slots:
        run_query(
            """
            MATCH (r:Recipe)
            WHERE r.recipe_id = $rid OR r.id = $rid
            MERGE (t:Tag {name: $slot})
            ON CREATE SET t.category = 'dish-type'
            ON MATCH  SET t.category = 'dish-type'
            MERGE (r)-[:HAS_TAG]->(t)
            """,
            {"rid": recipe_id, "slot": slot},
        )


# ---------------------------------------------------------------------------
# ElasticSearch helper (optional)
# ---------------------------------------------------------------------------

def sync_es(recipe_id: str, new_slots: list[str]) -> None:
    """Update the dish_type field in ES for *recipe_id*."""
    try:
        import requests as _req
        from recipe_wrangler.api.config import get_settings
        settings = get_settings()
        url = f"{settings.elastic_url}/{settings.elastic_index}/_update/{recipe_id}"
        payload = {"doc": {"dish_type": new_slots}}
        resp = _req.post(url, json=payload, timeout=10)
        if resp.status_code not in (200, 201):
            logger.warning("ES update failed for %s: %s", recipe_id, resp.text[:200])
    except Exception as exc:
        logger.warning("ES sync error for %s: %s", recipe_id, exc)


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

def classify_recipe(
    client,
    model: str,
    recipe: dict,
    retries: int = 3,
) -> list[str]:
    """Ask the LLM which slots this recipe belongs to.

    Returns a list of canonical slot strings (values from VALID_SLOTS).
    Falls back to the recipe's current_slots on repeated failure.
    """
    user_msg = (
        f'Title: "{recipe["title"]}"\n'
        f'Ingredients: "{recipe["ingredients"][:300]}"'
    )

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=256,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "meal_slots",
                        "strict": True,
                        "schema": SLOTS_SCHEMA,
                    },
                },
            )
            msg = response.choices[0].message
            raw = (msg.content or "").strip()
            # Fallback: some reasoning models stash the answer in reasoning/reasoning_content
            if not raw:
                raw = (getattr(msg, "reasoning_content", None)
                       or getattr(msg, "reasoning", None)
                       or "").strip()
            parsed = json.loads(raw)

            # Accept either {"slots": [...]} or a bare [...]
            if isinstance(parsed, dict):
                items = parsed.get("slots") or parsed.get("meal_slots") or []
            elif isinstance(parsed, list):
                items = parsed
            else:
                raise ValueError(f"Unexpected JSON type: {type(parsed)}")

            # Normalize to canonical slot names
            canonical: list[str] = []
            for item in items:
                slot = LLM_TO_SLOT.get(str(item).lower().strip())
                if slot and slot not in canonical:
                    canonical.append(slot)

            if canonical:
                return canonical

            logger.warning("LLM returned no recognizable slots for %r — raw: %s",
                           recipe["title"], raw)

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Parse error attempt %d for %r: %s",
                           attempt + 1, recipe["title"], exc)
        except Exception as exc:
            logger.warning("LLM error attempt %d for %r: %s",
                           attempt + 1, recipe["title"], exc)
            if "rate_limit" in str(exc).lower():
                time.sleep(2 ** attempt)

    # Fall back to current tags so we don't accidentally clear everything
    logger.error("All retries failed for %r — keeping current slots %s",
                 recipe["title"], recipe["current_slots"])
    return recipe["current_slots"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write",   action="store_true",
                        help="Persist changes to Neo4j (default: dry-run)")
    parser.add_argument("--sync-es", action="store_true",
                        help="Also update ElasticSearch dish_type field")
    parser.add_argument("--slot",    default=None,
                        help="Only re-classify recipes currently tagged with this slot "
                             "(e.g. 'breakfast')")
    parser.add_argument("--batch",   type=int, default=200,
                        help="Fetch this many recipes from Neo4j at a time (default 200)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel Groq workers (default 4)")
    parser.add_argument("--limit",   type=int, default=None,
                        help="Stop after processing this many recipes (for testing)")
    parser.add_argument("--backend", choices=["groq", "local"], default="groq",
                        help="LLM backend (default: groq)")
    parser.add_argument("--base-url", default="http://localhost:1234/v1",
                        help="OpenAI-compatible base URL for --backend local "
                             "(default LM Studio: http://localhost:1234/v1)")
    parser.add_argument("--model", default=None,
                        help="Override model name. Defaults: groq→GROQ_RETAG_MODEL env, "
                             "local→'openai/gpt-oss-20b'")
    args = parser.parse_args()

    if args.backend == "groq":
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        model = args.model or GROQ_MODEL
    else:
        from openai import OpenAI
        client = OpenAI(base_url=args.base_url, api_key="lm-studio")
        model = args.model or "openai/gpt-oss-20b"
    logger.info("Using backend=%s model=%s", args.backend, model)

    logger.info("Fetching recipes with dish-type tags from Neo4j (slot=%s)…", args.slot or "all")
    recipes = fetch_recipes_with_dish_type(slot=args.slot)
    logger.info("Found %d recipes to evaluate", len(recipes))

    if args.limit:
        recipes = recipes[: args.limit]
        logger.info("Limited to first %d recipes", len(recipes))

    changed = 0
    unchanged = 0
    errors = 0

    def process(recipe: dict) -> dict[str, Any]:
        new_slots = classify_recipe(client, model, recipe)
        return {"recipe": recipe, "new_slots": new_slots}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, r): r for r in recipes}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as exc:
                logger.error("Unexpected error: %s", exc)
                errors += 1
                continue

            recipe    = result["recipe"]
            new_slots = result["new_slots"]
            old_slots = sorted(recipe["current_slots"])
            new_sorted = sorted(new_slots)

            if old_slots == new_sorted:
                unchanged += 1
                if i % 50 == 0:
                    logger.info("[%d/%d] No change needed for %r",
                                i, len(recipes), recipe["title"])
                continue

            logger.info(
                "[%d/%d] %r  %s → %s%s",
                i, len(recipes),
                recipe["title"],
                old_slots,
                new_sorted,
                " (DRY RUN)" if not args.write else "",
            )

            if args.write:
                write_dish_type_tags(recipe["recipe_id"], new_slots)
                if args.sync_es:
                    sync_es(recipe["recipe_id"], new_slots)

            changed += 1

    logger.info(
        "\nDone. changed=%d  unchanged=%d  errors=%d  write_mode=%s",
        changed, unchanged, errors, args.write,
    )


if __name__ == "__main__":
    main()
