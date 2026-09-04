#!/usr/bin/env python3
"""Audit and safely normalize branded recipe ingredients.

The audit classifies every distinct graph ingredient, not only names from a
hard-coded brand dictionary. It writes a review CSV and never mutates data.
Examples:

    PYTHONPATH=src uv run python scripts/neo4j/clean_branded_ingredients.py audit \
      --names "farrah’s taco tortilla" "goat's cheese" "kraft grated parmesan cheese"

    PYTHONPATH=src uv run python scripts/neo4j/clean_branded_ingredients.py audit \
      --output artifacts/branded_ingredient_audit.csv

To apply reviewed rows, set ``approved=yes`` and ``reviewed_action`` to one of
``normalize``, ``keep``, or ``disable_recipe`` in the CSV. Applying is still a
dry-run unless ``--apply`` is supplied:

    PYTHONPATH=src uv run python scripts/neo4j/clean_branded_ingredients.py apply \
      --reviewed-csv artifacts/branded_ingredient_audit.csv

    PYTHONPATH=src uv run python scripts/neo4j/clean_branded_ingredients.py apply \
      --reviewed-csv artifacts/branded_ingredient_audit.csv --apply

``normalize`` keeps the recipe and rewires it to the generic Ingredient node.
The original branded node remains as provenance through ``NORMALIZED_TO``.
``disable_recipe`` is reversible and is only available after explicit review;
the script never hard-deletes a recipe or silently drops an edible ingredient.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.tools.parse_recipe_tool import _parser_llm  # noqa: E402
from recipe_wrangler.repositories.neo4j_recipes import set_recipe_status  # noqa: E402
from recipe_wrangler.utils.brand_normalization import (  # noqa: E402
    BRAND_CLASSIFICATION_VERSION,
    BrandIngredientBatch,
    BrandIngredientDecision,
    BrandReviewBatch,
    BrandReviewDecision,
    clean_generic_name,
    generic_name_is_valid,
    validate_brand_decision,
    validate_brand_review_decision,
)
from recipe_wrangler.catalog.projection import ProjectionError, project  # noqa: E402
from recipe_wrangler.utils.neo4j_utils import driver  # noqa: E402
from recipe_wrangler.utils.recipe_cache import cache_delete_many  # noqa: E402
from recipe_wrangler.utils.recipe_status import (  # noqa: E402
    STATUS_DISABLED,
    sync_recipe_status_to_es,
)

DEFAULT_OUTPUT = REPO_ROOT / "artifacts" / "branded_ingredient_audit.csv"

AUDIT_QUERY = """
MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE (size($sources) = 0 OR r.source IN $sources)
  AND (size($names) = 0 OR i.name IN $names)
WITH i, count(DISTINCT r) AS recipe_count,
     collect(DISTINCT coalesce(r.source, ""))[..8] AS sources,
     collect(DISTINCT {
       recipe_id: coalesce(toString(r.recipe_id), toString(r.id)),
       title: r.title,
       source: r.source
     })[..3] AS examples
CALL (i) {
  OPTIONAL MATCH (o:Ingredients_original)-[:MAPS_TO]->(i)
  RETURN collect(DISTINCT coalesce(o.original_text, o.name))[..3] AS originals
}
RETURN i.name AS ingredient_name, recipe_count, sources, examples, originals
ORDER BY recipe_count DESC, ingredient_name
"""

COLLISION_QUERY = """
MATCH (old:Ingredient {name: $old_name})
MATCH (clean:Ingredient {name: $generic_name})
MATCH (r:Recipe)-[:HAS_INGREDIENT]->(old)
WHERE (r)-[:HAS_INGREDIENT]->(clean)
RETURN count(DISTINCT r) AS collisions,
       collect(DISTINCT coalesce(toString(r.recipe_id), toString(r.id)))[..10]
         AS sample_ids
"""

USAGE_QUERY = """
MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient {name: $name})
RETURN count(DISTINCT r) AS recipes,
       collect(DISTINCT coalesce(toString(r.recipe_id), toString(r.id)))[..10]
         AS sample_ids
"""

REWRITE_QUERY = """
MATCH (old:Ingredient {name: $old_name})
MERGE (clean:Ingredient {name: $generic_name})
ON CREATE SET clean.canonical_id = randomUUID(),
              clean.source = coalesce(old.source, "brand-normalization"),
              clean.status = "resolved"
MERGE (old)-[norm:NORMALIZED_TO]->(clean)
SET norm.reason = "brand_removed",
    norm.brand_name = $brand_name,
    norm.classification_version = $classification_version,
    norm.updated_at = datetime(),
    old.status = "normalized_brand",
    old.normalized_brand_to = $generic_name
WITH old, clean
CALL (old, clean) {
  OPTIONAL MATCH (old)-[source_rel:SUITABILITY_FOR]->(group:ConsumerGroup)
  FOREACH (_ IN CASE WHEN source_rel IS NULL THEN [] ELSE [1] END |
    MERGE (clean)-[target_rel:SUITABILITY_FOR]->(group)
    ON CREATE SET target_rel = properties(source_rel)
  )
  RETURN count(source_rel) AS suitability_edges_seen
}
CALL (old, clean) {
  OPTIONAL MATCH (old)-[source_rel:HAS_CLASS]->(food_class:FoodOnClass)
  FOREACH (_ IN CASE WHEN source_rel IS NULL THEN [] ELSE [1] END |
    MERGE (clean)-[target_rel:HAS_CLASS]->(food_class)
    ON CREATE SET target_rel = properties(source_rel)
  )
  RETURN count(source_rel) AS foodon_edges_seen
}
CALL (old, clean) {
  OPTIONAL MATCH (old)-[source_rel:HAS_ALLERGEN]->(allergen:Allergen)
  FOREACH (_ IN CASE WHEN source_rel IS NULL THEN [] ELSE [1] END |
    MERGE (clean)-[target_rel:HAS_ALLERGEN]->(allergen)
    ON CREATE SET target_rel = properties(source_rel)
  )
  RETURN count(source_rel) AS allergen_edges_seen
}
WITH old, clean
MATCH (r:Recipe)-[h:HAS_INGREDIENT]->(old)
WITH old, clean, r, h, properties(h) AS props
CREATE (r)-[replacement:HAS_INGREDIENT]->(clean)
SET replacement = props,
    r.brand_cleanup_version = $classification_version
DELETE h
WITH DISTINCT old, clean, r
OPTIONAL MATCH (o:Ingredients_original)-[maps:MAPS_TO]->(old)
FOREACH (_ IN CASE WHEN maps IS NULL THEN [] ELSE [1] END |
  MERGE (o)-[:MAPS_TO]->(clean)
  DELETE maps
)
RETURN collect(DISTINCT coalesce(toString(r.recipe_id), toString(r.id)))
  AS recipe_ids
"""

REVIEW_COLUMNS = [
    "ingredient_name",
    "recipe_count",
    "sources",
    "example_recipes",
    "original_examples",
    "is_branded",
    "brand_name",
    "generic_name",
    "confidence",
    "recommended_action",
    "reason",
    "classification_version",
    "approved",
    "reviewed_action",
    "reviewer_notes",
]

SECOND_REVIEW_COLUMNS = [
    *REVIEW_COLUMNS,
    "second_review_verdict",
    "second_review_confidence",
    "second_review_reason",
]

SYSTEM_PROMPT = """You audit recipe ingredient labels for commercial brands.

For every input row, return exactly one decision with the exact ingredient_name.

Rules:
- A brand is a manufacturer, trademark, or commercial product name. Common food
  names, cuisines, people-derived food terms, and ordinary possessives are not
  automatically brands.
- If a brand is present and the generic edible food is clear, recommend
  `normalize` and return that generic food. Keep identity-changing qualifiers
  such as type, flavour, fat level, plant/animal basis, preservation state, and
  gluten-free status. Remove the manufacturer and branded product-line wording.
- Never remove the edible ingredient. "Farrah’s taco tortilla" becomes
  "taco tortilla"; "Kraft grated parmesan cheese" becomes
  "grated parmesan cheese"; "Coca-Cola" becomes "cola"; and
  "Kellogg's All-Bran cereal" (including a stored form such as
  "kellogg's all - bran cereal") becomes "bran cereal".
- Do not collapse an ingredient to a vague word such as sauce, syrup,
  seasoning, mix, spread, dressing, filling, product, beverage, or drink.
  If the label itself does not reveal a more specific generic identity,
  recommend `review`; do not infer it merely from familiarity with the brand.
- "goat's cheese" and "devil's food cake mix" are not brands.
- If a name is branded but its generic food identity is genuinely unclear,
  recommend `review`; do not invent a replacement and do not decide to delete.
- If it is not branded, recommend `keep`, with null brand_name/generic_name.
- Confidence is 0..1. Keep the reason short and factual.
"""

SECOND_REVIEW_PROMPT = """You are the strict independent reviewer of proposed
brand removals from recipe ingredients. The first classifier deliberately
over-detects brands. Review every row independently.

Return exactly one decision using the exact ingredient_name.

Choose `remove_brand` only when:
- the original ingredient contains a genuine manufacturer, trademark, or
  commercial product-line name; and
- the proposed/corrected generic name preserves the edible product and every
  identity-changing characteristic: food type, flavour, dietary basis,
  preparation, nutrition level, and form. This includes characteristics known
  from the branded product even when the stored label contains only the brand.
- the resulting name consists entirely of ordinary generic food words. Remove
  branded product-line names, slogans, and trademarked variety names too.

Do not approve a mapping merely because the result is an edible but broader
category. Ask whether the proposed generic could replace the original in the
same recipe and produce approximately the same food and nutrition. For example,
a plant-based branded meatball must remain a plant-based meatball, not merely a
meatball. A chocolate-hazelnut spread must retain both chocolate and hazelnut.
If you cannot identify the full generic product confidently, choose
`needs_review`.

Choose `keep_original` when the alleged brand is actually:
- a database/source name such as recipe1m, HealthyFoods, FoodHero, MyPlate, or
  Curated Irish Recipes;
- a generic food, food style, protected/traditional name, flavour, colour,
  preparation state, dietary property, or nutrition qualifier;
- examples include Monterey Jack, sharp cheddar, Parmigiano-Reggiano,
  vanilla ice cream, maraschino cherry, old-fashioned oats, light soy sauce,
  low-fat yogurt, ranch dressing, sriracha, Dijon mustard, and Worcestershire.

Choose `needs_review` when a real brand is plausible but the generic identity is
unclear, or when normalization would leave a vague word such as sauce, syrup,
seasoning, mix, spread, dressing, filling, product, beverage, or drink.

For `remove_brand`, return the corrected brand_name and generic_name.
Examples:
- Kraft grated parmesan cheese -> grated parmesan cheese
- Coca-Cola -> cola
- Cool Whip topping -> whipped topping
- Quorn meatball -> plant-based meatball
- Nutella -> chocolate hazelnut spread
- Kellogg's All-Bran cereal -> bran cereal
- Hershey's syrup -> needs_review because the label does not state syrup type

Do not infer a specific product merely from familiarity with a brand.
"""


def _classify_batch(rows: list[dict[str, Any]]) -> list[BrandIngredientDecision]:
    model_name = (
        os.getenv("BRAND_NORMALIZATION_LLM")
        or os.getenv("PARSE_LLM")
        or "openai/gpt-4o-mini"
    ).strip()
    llm, method = _parser_llm(model_name)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", "Classify these ingredient rows:\n{rows_json}"),
        ]
    )
    chain = prompt | llm.with_structured_output(
        BrandIngredientBatch,
        method=method,
    )
    payload = json.dumps(rows, ensure_ascii=False)
    try:
        result = chain.invoke({"rows_json": payload})
    except Exception as exc:
        if method == "json_schema" and "response format `json_schema`" in str(exc):
            fallback = prompt | llm.with_structured_output(
                BrandIngredientBatch,
                method="function_calling",
            )
            result = fallback.invoke({"rows_json": payload})
        else:
            raise

    by_name = {
        str(d.ingredient_name).strip().casefold(): d
        for d in result.decisions
    }
    decisions: list[BrandIngredientDecision] = []
    for row in rows:
        name = str(row["ingredient_name"]).strip()
        decision = by_name.get(name.casefold())
        if decision is None:
            decision = BrandIngredientDecision(
                ingredient_name=name,
                is_branded=False,
                confidence=0.0,
                recommended_action="review",
                reason="classifier omitted this ingredient",
            )
        decisions.append(validate_brand_decision(name, decision))
    return decisions


def _review_batch(rows: list[dict[str, Any]]) -> list[BrandReviewDecision]:
    model_name = (
        os.getenv("BRAND_REVIEW_LLM")
        or os.getenv("BRAND_NORMALIZATION_LLM")
        or os.getenv("PARSE_LLM")
        or "openai/gpt-4o-mini"
    ).strip()
    llm, method = _parser_llm(model_name)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SECOND_REVIEW_PROMPT),
            ("human", "Review these proposed mappings:\n{rows_json}"),
        ]
    )
    chain = prompt | llm.with_structured_output(BrandReviewBatch, method=method)
    payload = json.dumps(rows, ensure_ascii=False)
    try:
        result = chain.invoke({"rows_json": payload})
    except Exception as exc:
        if method == "json_schema" and "response format `json_schema`" in str(exc):
            fallback = prompt | llm.with_structured_output(
                BrandReviewBatch,
                method="function_calling",
            )
            result = fallback.invoke({"rows_json": payload})
        else:
            raise

    by_name = {
        str(d.ingredient_name).strip().casefold(): d
        for d in result.decisions
    }
    decisions: list[BrandReviewDecision] = []
    for row in rows:
        name = str(row["ingredient_name"]).strip()
        decision = by_name.get(name.casefold())
        if decision is None:
            decision = BrandReviewDecision(
                ingredient_name=name,
                verdict="needs_review",
                confidence=0.0,
                reason="reviewer omitted this ingredient",
            )
        decisions.append(validate_brand_review_decision(name, decision))
    return decisions


def _fetch_audit_rows(
    *,
    sources: list[str],
    names: list[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    with driver.session() as session:
        rows = [
            dict(row)
            for row in session.run(
                AUDIT_QUERY,
                sources=sources,
                names=names,
            )
        ]
    return rows[:limit] if limit else rows


def _write_review_csv(
    path: Path,
    rows: list[dict[str, Any]],
    decisions: list[BrandIngredientDecision],
    *,
    include_kept: bool,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for row, decision in zip(rows, decisions):
            if decision.recommended_action == "keep" and not include_kept:
                continue
            writer.writerow(
                {
                    "ingredient_name": row["ingredient_name"],
                    "recipe_count": row["recipe_count"],
                    "sources": json.dumps(row.get("sources") or [], ensure_ascii=False),
                    "example_recipes": json.dumps(
                        row.get("examples") or [], ensure_ascii=False
                    ),
                    "original_examples": json.dumps(
                        row.get("originals") or [], ensure_ascii=False
                    ),
                    "is_branded": str(decision.is_branded).lower(),
                    "brand_name": decision.brand_name or "",
                    "generic_name": decision.generic_name or "",
                    "confidence": f"{decision.confidence:.3f}",
                    "recommended_action": decision.recommended_action,
                    "reason": decision.reason,
                    "classification_version": BRAND_CLASSIFICATION_VERSION,
                    "approved": "",
                    "reviewed_action": "",
                    "reviewer_notes": "",
                }
            )
            written += 1
    return written


def audit(args: argparse.Namespace) -> int:
    rows = _fetch_audit_rows(
        sources=args.sources,
        names=args.names,
        limit=args.limit,
    )
    if not rows:
        print("No matching graph ingredients.")
        return 0

    output = Path(args.output).resolve()
    checkpoint = (
        Path(args.checkpoint).resolve()
        if args.checkpoint
        else output.with_suffix(".checkpoint.jsonl")
    )
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    decisions_by_name: dict[str, BrandIngredientDecision] = {}
    if args.resume and checkpoint.exists():
        with checkpoint.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                decision = BrandIngredientDecision.model_validate_json(line)
                decisions_by_name[decision.ingredient_name.casefold()] = decision
        print(f"resumed {len(decisions_by_name)} classification(s) from {checkpoint}")
    else:
        checkpoint.write_text("", encoding="utf-8")

    pending = [
        row
        for row in rows
        if str(row["ingredient_name"]).casefold() not in decisions_by_name
    ]
    batches: list[list[dict[str, Any]]] = []
    for start in range(0, len(pending), args.batch_size):
        batches.append(
            [
                {
                    "ingredient_name": row["ingredient_name"],
                    "recipe_count": row["recipe_count"],
                    "sources": row.get("sources") or [],
                    "original_examples": row.get("originals") or [],
                }
                for row in pending[start : start + args.batch_size]
            ]
        )

    completed = len(rows) - len(pending)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_classify_batch, batch): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch_decisions = future.result()
            with checkpoint.open("a", encoding="utf-8") as handle:
                for decision in batch_decisions:
                    decisions_by_name[decision.ingredient_name.casefold()] = decision
                    handle.write(decision.model_dump_json() + "\n")
            completed += len(batch_decisions)
            print(f"classified {completed}/{len(rows)}", flush=True)

    decisions = [
        decisions_by_name[str(row["ingredient_name"]).casefold()]
        for row in rows
    ]
    written = _write_review_csv(
        output,
        rows,
        decisions,
        include_kept=args.include_kept,
    )
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.recommended_action] = (
            counts.get(decision.recommended_action, 0) + 1
        )
    print(f"classification counts: {counts}")
    print(f"review rows written: {written}")
    print(f"review CSV: {output}")
    print(f"resume checkpoint: {checkpoint}")
    print("No database or Elasticsearch changes were made.")
    return 0


def _approved_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    approved: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("approved") or "").strip().casefold() not in {
            "1",
            "true",
            "yes",
            "y",
        }:
            continue
        action = str(row.get("reviewed_action") or "").strip().casefold()
        if action not in {"normalize", "keep", "disable_recipe"}:
            raise ValueError(
                f"Approved row {row.get('ingredient_name')!r} needs an explicit "
                "reviewed_action: normalize, keep, or disable_recipe"
            )
        row["reviewed_action"] = action
        approved.append(row)
    return approved


def review_audit(args: argparse.Namespace) -> int:
    input_path = Path(args.audit_csv).resolve()
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print("Audit CSV has no candidate rows.")
        return 0

    output = Path(args.output).resolve()
    checkpoint = (
        Path(args.checkpoint).resolve()
        if args.checkpoint
        else output.with_suffix(".checkpoint.jsonl")
    )
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    decisions_by_name: dict[str, BrandReviewDecision] = {}
    if args.resume and checkpoint.exists():
        with checkpoint.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                decision = BrandReviewDecision.model_validate_json(line)
                decisions_by_name[decision.ingredient_name.casefold()] = decision
        print(f"resumed {len(decisions_by_name)} review(s) from {checkpoint}")
    else:
        checkpoint.write_text("", encoding="utf-8")

    pending = [
        row
        for row in rows
        if str(row["ingredient_name"]).casefold() not in decisions_by_name
    ]
    batches: list[list[dict[str, Any]]] = []
    for start in range(0, len(pending), args.batch_size):
        batches.append(
            [
                {
                    "ingredient_name": row["ingredient_name"],
                    "recipe_count": int(row.get("recipe_count") or 0),
                    "proposed_brand": row.get("brand_name") or None,
                    "proposed_generic": row.get("generic_name") or None,
                    "first_action": row.get("recommended_action"),
                }
                for row in pending[start : start + args.batch_size]
            ]
        )

    completed = len(rows) - len(pending)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_review_batch, batch): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch_decisions = future.result()
            with checkpoint.open("a", encoding="utf-8") as handle:
                for decision in batch_decisions:
                    decisions_by_name[decision.ingredient_name.casefold()] = decision
                    handle.write(decision.model_dump_json() + "\n")
            completed += len(batch_decisions)
            print(f"reviewed {completed}/{len(rows)}", flush=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SECOND_REVIEW_COLUMNS)
        writer.writeheader()
        for row in rows:
            decision = validate_brand_review_decision(
                row["ingredient_name"],
                decisions_by_name[row["ingredient_name"].casefold()],
            )
            counts[decision.verdict] = counts.get(decision.verdict, 0) + 1
            out = dict(row)
            out["second_review_verdict"] = decision.verdict
            out["second_review_confidence"] = f"{decision.confidence:.3f}"
            out["second_review_reason"] = decision.reason
            out["brand_name"] = decision.brand_name or row.get("brand_name", "")
            out["generic_name"] = decision.generic_name or row.get("generic_name", "")
            if decision.verdict == "remove_brand":
                out["approved"] = "yes"
                out["reviewed_action"] = "normalize"
            elif decision.verdict == "keep_original":
                out["approved"] = "yes"
                out["reviewed_action"] = "keep"
            else:
                out["approved"] = ""
                out["reviewed_action"] = ""
            out["reviewer_notes"] = (
                f"second review {decision.confidence:.3f}: {decision.reason}"
            ).strip()
            writer.writerow(out)

    print(f"second-review counts: {counts}")
    print(f"reviewed CSV: {output}")
    print(f"resume checkpoint: {checkpoint}")
    print("No database or Elasticsearch changes were made.")
    return 0


def _preflight(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    normalizations: list[dict[str, str]] = []
    disable_names: list[str] = []
    seen: dict[str, tuple[str, str]] = {}
    collision_errors: list[str] = []
    with driver.session() as session:
        for row in rows:
            action = row["reviewed_action"]
            name = str(row.get("ingredient_name") or "").strip()
            if action == "keep":
                continue
            if not name:
                raise ValueError("Approved row has an empty ingredient_name")
            signature = (action, str(row.get("generic_name") or "").strip())
            if name in seen and seen[name] != signature:
                raise ValueError(f"Conflicting approved actions for {name!r}")
            seen[name] = signature

            usage = session.run(USAGE_QUERY, name=name).single()
            count = int(usage["recipes"] or 0) if usage else 0
            if count == 0:
                print(f"skip missing/unused ingredient: {name!r}")
                continue

            if action == "disable_recipe":
                disable_names.append(name)
                print(f"disable_recipe: {name!r} affects {count} recipe(s)")
                continue

            generic = str(row.get("generic_name") or "").strip().lower()
            brand = str(row.get("brand_name") or "").strip()
            if not generic_name_is_valid(name, generic, brand):
                raise ValueError(
                    f"Unsafe generic mapping for {name!r}: {generic!r}"
                )
            collision = session.run(
                COLLISION_QUERY,
                old_name=name,
                generic_name=generic,
            ).single()
            collisions = int(collision["collisions"] or 0) if collision else 0
            if collisions:
                collision_errors.append(
                    f"{name!r} -> {generic!r} collides in {collisions} recipe(s); "
                    f"sample={collision['sample_ids']}"
                )
                continue
            normalizations.append(
                {
                    "old_name": name,
                    "generic_name": generic,
                    "brand_name": brand,
                    "recipes": str(count),
                }
            )
            print(f"normalize: {name!r} -> {generic!r} ({count} recipe(s))")
    if collision_errors:
        details = "\n".join(f"- {error}" for error in collision_errors)
        raise ValueError(
            "Ingredient relationship collisions require review before apply:\n"
            f"{details}"
        )
    return normalizations, disable_names


def apply_reviewed(args: argparse.Namespace) -> int:
    reviewed_csv = Path(args.reviewed_csv).resolve()
    rows = _approved_rows(reviewed_csv)
    if not rows:
        print("No approved rows. Nothing to do.")
        return 0
    normalizations, disable_names = _preflight(rows)
    print(
        f"approved plan: {len(normalizations)} normalization(s), "
        f"{len(disable_names)} disable rule(s)"
    )
    if not args.apply:
        print("Dry-run only. Add --apply after reviewing the plan.")
        return 0

    affected_ids: set[str] = set()
    with driver.session() as session:
        for row in normalizations:
            result = session.run(
                REWRITE_QUERY,
                old_name=row["old_name"],
                generic_name=row["generic_name"],
                brand_name=row["brand_name"],
                classification_version=BRAND_CLASSIFICATION_VERSION,
            ).single()
            affected_ids.update(
                str(recipe_id)
                for recipe_id in ((result and result["recipe_ids"]) or [])
                if recipe_id
            )

    disabled_ids: list[str] = []
    if disable_names:
        with driver.session() as session:
            for name in disable_names:
                records = session.run(
                    """
                    MATCH (r:Recipe)-[:HAS_INGREDIENT]->(:Ingredient {name: $name})
                    RETURN DISTINCT coalesce(toString(r.recipe_id), toString(r.id))
                      AS recipe_id
                    """,
                    name=name,
                )
                disabled_ids.extend(
                    str(record["recipe_id"])
                    for record in records
                    if record["recipe_id"]
                )
        disabled_ids = sorted(set(disabled_ids))
        set_recipe_status(
            disabled_ids,
            STATUS_DISABLED,
            "brand cleanup: no reviewed generic equivalent",
        )

    affected_ids.update(disabled_ids)
    cache_delete_many(affected_ids)

    if not args.skip_es:
        from recipe_wrangler.api.config import get_settings

        settings = get_settings()
        if disabled_ids:
            sync_recipe_status_to_es(
                disabled_ids,
                STATUS_DISABLED,
                es_url=settings.elastic_url,
                indices=[settings.elastic_index],
            )
        projected = 0
        for index, recipe_id in enumerate(
            sorted(affected_ids - set(disabled_ids)),
            start=1,
        ):
            try:
                project(recipe_id)
                projected += 1
            except ProjectionError as exc:
                print(f"Elasticsearch projection failed for {recipe_id}: {exc}")
            if index % 250 == 0:
                print(f"Elasticsearch projection {index}/{len(affected_ids)}")
        print(f"Elasticsearch projected: {projected}")
    else:
        print("Elasticsearch sync skipped by request.")

    print(f"affected recipes: {len(affected_ids)}")
    print(f"disabled recipes: {len(disabled_ids)}")
    print(
        "After a large normalization run, rerun allergen/FATO enrichment and "
        "vegan/vegetarian classification for complete derived evidence."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    audit_parser = sub.add_parser("audit", help="Classify ingredients and write review CSV")
    audit_parser.add_argument("--sources", nargs="*", default=[])
    audit_parser.add_argument("--names", nargs="*", default=[])
    audit_parser.add_argument("--limit", type=int)
    audit_parser.add_argument("--batch-size", type=int, default=25)
    audit_parser.add_argument("--workers", type=int, default=1)
    audit_parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    audit_parser.add_argument("--checkpoint")
    audit_parser.add_argument("--resume", action="store_true")
    audit_parser.add_argument("--include-kept", action="store_true")
    audit_parser.set_defaults(func=audit)

    review_parser = sub.add_parser(
        "review",
        help="Independently review every first-pass candidate",
    )
    review_parser.add_argument("--audit-csv", required=True)
    review_parser.add_argument("--output", required=True)
    review_parser.add_argument("--checkpoint")
    review_parser.add_argument("--resume", action="store_true")
    review_parser.add_argument("--batch-size", type=int, default=50)
    review_parser.add_argument("--workers", type=int, default=1)
    review_parser.set_defaults(func=review_audit)

    apply_parser = sub.add_parser("apply", help="Apply explicitly reviewed CSV rows")
    apply_parser.add_argument("--reviewed-csv", required=True)
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.add_argument("--skip-es", action="store_true")
    apply_parser.set_defaults(func=apply_reviewed)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if getattr(args, "batch_size", 1) < 1:
        raise ValueError("--batch-size must be at least 1")
    if getattr(args, "workers", 1) < 1:
        raise ValueError("--workers must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
