#!/usr/bin/env python3
"""Annotate recipes with cuisine, flavour and mood — vocabulary-first.

The vocabulary comes before the annotation. Every facet is constrained to a
closed list sourced from what the system already knows (``catalog/vocabularies``),
and anything a model returns outside that list is discarded rather than
coerced. Without that, one recipe gets ``italian``, the next ``Italian
cuisine``, the third ``mediterranean/italian``, and the facet is useless for
browsing — which is the state the corpus is in today.

Two passes, deliberately separate:

**Derived pass** (no model, deterministic, repeatable)
    ``food_groups`` from each recipe's FoodOn class ancestry, already
    denormalized onto the document as ``ingredient_class_ancestors``. Writes the
    human-authoritative field because it is data, not judgement.

**Model pass** (constrained, audited, reversible)
    ``cuisines``, ``flavor_profiles`` and ``moods``. Values are written to the
    facet itself, not to an ``ai_`` twin: ``annotation_evidence`` records method, confidence and
    vocabulary version per value, and ``enhancements[].before`` retains whatever
    was replaced, so provenance and reversibility do not need a second field.

Usage
-----
  # See the vocabulary and the exact prompt, without touching anything
  python scripts/catalog/annotate_recipes.py --show-vocabulary
  python scripts/catalog/annotate_recipes.py --show-prompt --limit 1

  # Derived pass only — no model, no API key needed
  python scripts/catalog/annotate_recipes.py --facets food_groups --apply

  # Only recipes that have no cuisine yet
  python scripts/catalog/annotate_recipes.py --missing cuisines --apply

  # Re-annotate one recipe
  python scripts/catalog/annotate_recipes.py --recipe-id 0000656901 --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.catalog import vocabularies as V
# Derivation lives in the catalog core so the commit path shares it.
from recipe_wrangler.catalog.annotation import derive_food_groups
from recipe_wrangler.catalog.elastic import get_catalog_client
from recipe_wrangler.catalog.entities import recipe_entity

logger = logging.getLogger("annotate_recipes")

MODEL_FACETS = ("cuisines", "flavor_profiles", "moods")
DERIVED_FACETS = ("food_groups",)


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def vocabulary_block() -> str:
    """Render the closed vocabularies the model must choose from."""
    lines: list[str] = []
    for facet in MODEL_FACETS:
        spec = V.FACETS[facet]
        values, description = spec["values"], spec["description"]
        lines.append(f"{facet} — {description}")
        lines.append(f"  allowed values: {', '.join(values)}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You classify recipes into a fixed set of facets.

Rules, in order of importance:
1. Use ONLY values from the allowed lists below. Never invent a value, never
   return a variant spelling, never combine two values with a slash.
2. If the evidence does not clearly support a value, return an empty list for
   that facet. An empty answer is correct and useful; a guess is not.
3. Judge the dish as a whole, from its title and ingredients. Do not infer a
   cuisine from a single ingredient — olive oil does not make a dish Italian,
   and soy sauce does not make it Chinese.
4. Assign at most 2 cuisines, 4 flavor_profiles and 2 moods.
5. confidence is your own 0-1 estimate that the whole assignment is right.

{vocabulary_block()}

Respond with JSON only, no prose:
{{"cuisines": [], "flavor_profiles": [], "moods": [],
  "confidence": 0.0}}"""


def build_user_prompt(doc: dict[str, Any]) -> str:
    """The per-recipe half of the prompt.

    Includes a source hint where the source's geography is known, because that
    is real evidence — but it is offered as a prior, not an answer, so the model
    can still say a Hungarian-sourced curry is not Hungarian.
    """
    parts = [f"Title: {doc.get('title', '')}"]

    ingredients = doc.get("ingredient_names") or []
    if ingredients:
        parts.append(f"Ingredients: {', '.join(ingredients[:40])}")

    if doc.get("description"):
        parts.append(f"Description: {str(doc['description'])[:400]}")

    prior = V.SOURCE_CUISINE_PRIOR.get(doc.get("source", ""))
    if prior:
        parts.append(
            f"Source hint: this recipe comes from a {prior} collection. Treat as"
            " a prior, not a certainty."
        )

    tags = [t for t in (doc.get("tags") or []) if not t.startswith(("source:", "type:"))]
    if tags:
        parts.append(f"Existing tags: {', '.join(tags[:15])}")

    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Derived pass
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Model pass
# --------------------------------------------------------------------------- #
def call_model(
    user_prompt: str, *, model: str, temperature: float, attempts: int = 4
) -> dict[str, Any]:
    """Ask the configured LLM for one recipe's annotation.

    Retries with exponential backoff. Concurrency pushes Groq into rate-limiting
    (429) and the occasional truncated response, and without retries those
    surface as permanent failures — a run at 8 workers lost 569 of 7,221
    recipes (7.9%) that way, silently keeping their old values.
    """
    from langchain_groq import ChatGroq

    llm = ChatGroq(model=model, temperature=temperature)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", user_prompt)])
            text = str(getattr(response, "content", response)).strip()
            if text.startswith("```"):
                text = text.split("```")[1].removeprefix("json").strip()
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == attempts - 1:
                break
            # 1s, 2s, 4s — long enough for a per-minute token bucket to refill.
            time.sleep(2**attempt)
    raise last if last else RuntimeError("model call failed")


def annotate_one(
    doc: dict[str, Any],
    *,
    model: str,
    temperature: float,
    facets: tuple[str, ...] = MODEL_FACETS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (ai_fields, evidence) for one recipe, vocabulary-validated.

    Course types are intentionally outside this model pass. Existing values are
    preserved by catalog projection and missing values remain empty.
    """
    raw = call_model(
        build_user_prompt(doc),
        model=model,
        temperature=temperature,
    )
    confidence = raw.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = None

    fields: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []

    # Only the requested facets are written. The model is still *asked* for all
    # of them in one call — splitting the prompt would cost four calls per
    # recipe for no benefit — but `--facets cuisines` must not silently
    # overwrite unrelated facets as it previously did.
    for facet in facets:
        proposed = raw.get(facet) or []
        values = V.validate_values(facet, proposed)
        if not values:
            continue
        # Written to the facet itself, not an `ai_` twin. What produced each
        # value lives in `annotation_evidence`, and `enhancements[].before`
        # keeps whatever was replaced — so provenance and reversibility survive
        # without a second field per concept.
        fields[facet] = values
        evidence.extend(
            V.evidence_entry(facet, value, method="model", confidence=confidence)
            for value in values
        )
    return fields, evidence


# --------------------------------------------------------------------------- #
def iter_targets(
    entity,
    *,
    recipe_id: str | None,
    missing: str | None,
    limit: int | None,
    missing_enhancement: str | None = None,
) -> Iterator[dict[str, Any]]:
    if recipe_id:
        doc = entity.get(recipe_id)
        if doc:
            yield doc
        return

    query: dict[str, Any] = {"bool": {"must_not": [{"terms": {"status": ["disabled", "deleted"]}}]}}
    if missing:
        query["bool"]["must_not"].append({"exists": {"field": missing}})
    if missing_enhancement:
        # Recipes with no audit entry for this field — i.e. the ones a previous
        # run never successfully wrote. Targets stragglers precisely, without
        # re-paying for the ~90% that already succeeded.
        query["bool"]["must_not"].append(
            {
                "nested": {
                    "path": "enhancements",
                    "query": {"term": {"enhancements.fields": missing_enhancement}},
                }
            }
        )

    count = 0
    for hit in entity.scroll_all(query=query):
        yield hit["_source"]
        count += 1
        if limit is not None and count >= limit:
            return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--facets", help=f"Comma-separated subset of {DERIVED_FACETS + MODEL_FACETS}")
    ap.add_argument("--recipe-id", help="Annotate a single recipe.")
    ap.add_argument("--missing", help="Only recipes lacking this facet.")
    ap.add_argument("--limit", type=int)
    ap.add_argument(
        "--retry-missing",
        metavar="FIELD",
        help="Only recipes with no enhancement audit entry for FIELD — i.e. the "
             "ones a previous run failed on. e.g. --retry-missing cuisines",
    )
    # Groq, per the rest of the stack. Deliberately NOT SEARCH_MAIN_MODEL:
    # that setting is shared with query-time constraint extraction, wants a fast
    # small model, and its current default (meta-llama/llama-4-scout-17b-16e-
    # instruct) returns 404 model_not_found on this workspace's key. Annotation
    # is an offline batch job that wants the larger model — the 8b instant one
    # returns noticeably more out-of-vocabulary values, which are then discarded
    # and the call wasted.
    ap.add_argument(
        "--model",
        default=os.getenv("ANNOTATION_MODEL", "llama-3.3-70b-versatile"),
        help="Groq model id (env: ANNOTATION_MODEL).",
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("ANNOTATION_WORKERS", "8")),
        help="Concurrent Groq calls (env: ANNOTATION_WORKERS). Lower if rate-limited.",
    )
    ap.add_argument("--show-vocabulary", action="store_true")
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    if args.show_vocabulary:
        print(f"classification_version: {V.CLASSIFICATION_VERSION}\n")
        for facet, spec in V.FACETS.items():
            print(f"{facet} ({len(spec['values'])}, method={spec['method']}, "
                  f"authoritative={spec['authoritative']})")
            print(f"  {', '.join(spec['values'])}\n")
        return

    requested = (
        [f.strip() for f in args.facets.split(",")]
        if args.facets
        else list(DERIVED_FACETS + MODEL_FACETS)
    )
    model_facets = tuple(f for f in MODEL_FACETS if f in requested)
    want_model = bool(model_facets)
    want_derived = any(f in DERIVED_FACETS for f in requested)
    if want_model:
        logger.info("model facets to write: %s", ", ".join(model_facets))

    get_catalog_client()
    entity = recipe_entity()
    stats = Counter()
    value_hist: Counter = Counter()

    targets = list(
        iter_targets(
            entity,
            recipe_id=args.recipe_id,
            missing=args.missing,
            limit=args.limit,
            missing_enhancement=args.retry_missing,
        )
    )
    logger.info("%s recipe(s) to process", len(targets))

    if args.show_prompt:
        for doc in targets[: args.limit or 1]:
            print("=" * 70)
            print(SYSTEM_PROMPT)
            print("-" * 70)
            print(build_user_prompt(doc))
        return

    for doc in targets:
        stats["seen"] += 1
        urn = doc.get("urn")

        # --- derived ---------------------------------------------------- #
        if want_derived:
            groups, evidence = derive_food_groups(doc)
            if groups:
                stats["food_groups_derived"] += 1
                for g in groups:
                    value_hist[f"food_groups:{g}"] += 1
                if args.apply:
                    existing = [
                        e for e in (doc.get("annotation_evidence") or [])
                        if e.get("facet") != "food_groups"
                    ]
                    entity.patch(
                        urn,
                        {
                            "food_groups": groups,
                            "annotation_evidence": existing + evidence,
                        },
                        refresh="false",
                        reread=False,
                    )

    # --- model pass ------------------------------------------------------ #
    #
    # Run concurrently: each recipe is one independent Groq call, so the job is
    # bound by network round-trip, not by CPU or by Elasticsearch. Sequentially
    # 7,221 recipes take roughly two hours; a modest pool brings that to
    # minutes. Groq's rate limit is the real ceiling — back off --workers if the
    # error count climbs.
    if want_model and targets:
        def process(doc: dict[str, Any]) -> tuple[str, dict[str, Any], list, Exception | None]:
            urn = doc.get("urn")
            try:
                fields, evidence = annotate_one(
                    doc,
                    model=args.model,
                    temperature=args.temperature,
                    facets=model_facets,
                )
                return urn, fields, evidence, None
            except Exception as exc:  # noqa: BLE001
                return urn, {}, [], exc

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for urn, fields, evidence, error in pool.map(process, targets):
                done += 1
                if done % 250 == 0:
                    logger.info("annotated %s/%s", done, len(targets))
                if error is not None:
                    stats["model_errors"] += 1
                    if stats["model_errors"] <= 5:
                        logger.warning("annotation failed for %s: %s", urn, error)
                    continue
                if not fields:
                    stats["model_empty"] += 1
                    continue
                stats["model_annotated"] += 1
                for key, values in fields.items():
                    for value in values:
                        value_hist[f"{key}:{value}"] += 1
                if args.apply:
                    fresh = entity.get(urn) or {}
                    kept = [
                        e for e in (fresh.get("annotation_evidence") or [])
                        if e.get("method") != "model"
                    ]
                    entity.enhance(
                        urn,
                        fields={**fields, "annotation_evidence": kept + evidence},
                        agent=f"recipe-annotator/{V.CLASSIFICATION_VERSION}",
                        refresh="false",
                        reread=False,
                    )

    # One refresh at the end rather than per document (see Entity.patch).
    if args.apply:
        entity.es.refresh(entity.alias)

    logger.info("--- summary ---")
    for key, value in sorted(stats.items()):
        logger.info("%-22s %s", key, value)
    if value_hist:
        logger.info("assigned values:")
        for key, count in value_hist.most_common(40):
            logger.info("  %-40s %s", key, count)
    if not args.apply:
        logger.info("no --apply — nothing written.")


if __name__ == "__main__":
    main()
