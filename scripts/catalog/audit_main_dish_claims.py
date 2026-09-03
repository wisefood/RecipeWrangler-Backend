#!/usr/bin/env python3
"""Audit recipes that claim ``main-dish`` and demote the ones that cannot be one.

Why this exists
---------------
Members report sauces being served as dinner. The cause is in the corpus, not
the planner: ``plan_meals`` maps the lunch/dinner slots onto
``course_types: main-dish`` (``api/routers/tools.py:SLOT_COURSE_TYPES``) and
filters Elasticsearch on it, so anything wearing that label is a candidate
main course. 4,674 of 7,221 recipes wear it — including "Pasta sauce",
"Bolognese sauce" and "Marinara Sauce".

Two obvious approaches are both wrong, in opposite directions:

*Demote by title.* Most ``main-dish`` recipes whose title contains "sauce" are
genuine main dishes — "Pork fillet in red wine and cranberry sauce", "Chicken
in barbecue sauce". A title rule would demote ~130 correct recipes to fix ~10,
which is the mistake ``catalog/entities.py`` already warns about: it discarded
paid annotations during the v4 rebuild, "notably recipes whose titles end in
'sauce' or 'marinade'".

*Audit only the double-tagged ``side`` + ``main-dish`` recipes.* That set is 57
documents corpus-wide and just 4 of them are sauce-titled. "Bolognese sauce" is
tagged ``main-dish`` and nothing else, so the double-tag filter cannot see it.

So the title vocabulary is used only to *nominate* candidates, and a model
decides each one. A false nomination is cheap — the model confirms the recipe is
a main dish and nothing is written. That keeps recall high without letting a
keyword rule touch the data.

What it writes
--------------
Only ``course_types`` and the ``annotation_evidence`` rows for that facet, via
``Entity.enhance`` — so ``enhancements[].before`` retains the previous value and
every change is reversible and attributable. ``main-dish`` is removed only when
the model affirmatively rejects it *and* offers a valid replacement; the field is
never left empty.

Elasticsearch is the right and only target: ``catalog/entities.py`` states course
labels are Elasticsearch-owned annotations. Note that ``tools/param_search.py``
still reads dish-type Tags from Neo4j, so that endpoint keeps its own copy —
see ``scripts/neo4j/retag_dish_types_llm.py`` for the graph side.

Usage
-----
  # Dry run over the default candidate set; writes a review report, touches nothing
  python3 scripts/catalog/audit_main_dish_claims.py

  # Andrea's narrower question: only the side + main-dish double-tagged recipes
  python3 scripts/catalog/audit_main_dish_claims.py --double-tagged-only

  # Spot-check one recipe
  python3 scripts/catalog/audit_main_dish_claims.py --recipe-id 7707973868

  # Review the report, then apply
  python3 scripts/catalog/audit_main_dish_claims.py --workers 8 --apply

  # Against a local OpenAI-compatible server instead of Groq
  python3 scripts/catalog/audit_main_dish_claims.py \\
      --backend local --model openai/gpt-oss-20b
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.catalog import sources as S
from recipe_wrangler.catalog import vocabularies as V
from recipe_wrangler.catalog.entities import recipe_entity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AGENT = f"main-dish-auditor/{V.CLASSIFICATION_VERSION}"
DEFAULT_MODEL = os.getenv("ANNOTATION_MODEL", "llama-3.3-70b-versatile")

# Words that *nominate* a recipe for review, never demote it. Matched as a
# phrase against the analysed title, so "sauce" also reaches "Pasta sauce" but
# these are candidates for the model, not verdicts.
ACCOMPANIMENT_WORDS: tuple[str, ...] = (
    "sauce", "marinade", "dressing", "dip", "glaze", "pesto", "salsa",
    "chutney", "relish", "condiment", "gravy", "aioli", "mayonnaise",
    "vinaigrette", "syrup", "spread", "paste", "rub", "stock", "broth",
    "butter", "jam", "pickle", "topping", "seasoning",
)

# A main dish built from three ingredients is unusual enough to be worth a look.
THIN_INGREDIENT_COUNT = 4

SYSTEM_PROMPT = f"""You decide whether a recipe can stand on its own as a main dish.

A main dish is the centre of a meal: someone eats a plate of it and that is
their lunch or dinner. An accompaniment is not — a sauce, dressing, dip,
marinade, condiment, spread or stock is eaten *with* something else, however
substantial its ingredient list.

Judge the recipe as written. Be careful in both directions:
- "Chicken in barbecue sauce" IS a main dish. The sauce is a component; the
  recipe produces a plate of chicken. The word "sauce" in a title proves nothing.
- "Bolognese sauce" or "Pasta sauce" is NOT a main dish, even though it is
  savoury, cooked and filling. It produces a sauce; the meal needs pasta.
- A salad, soup or starter substantial enough to be a meal MAY be both. Say so.

Then assign the courses the recipe really belongs to, using ONLY these values:
{', '.join(S.COURSE_TYPES)}

There is no "sauce" or "condiment" value in that list. For an accompaniment,
use "side" — it is the closest available course. Assign at most 2 values, and
never return an empty list.

confidence is your own 0-1 estimate that the whole verdict is right.

Respond with JSON only, no prose:
{{"standalone_main_dish": true, "course_types": [], "reason": "", "confidence": 0.0}}"""


def build_user_prompt(doc: dict[str, Any]) -> str:
    """The per-recipe half of the prompt.

    The stored ``course_types`` is deliberately withheld: it is exactly what is
    under suspicion, and stating it anchors the model to the answer we are
    trying to check — the same trap ``annotate_recipes.py`` documents, where a
    brownie came back "main-dish" because the prompt said it already was one.
    """
    parts = [f"Title: {doc.get('title', '')}"]

    ingredients = doc.get("ingredient_names") or []
    if ingredients:
        parts.append(f"Ingredients: {', '.join(ingredients[:40])}")
    if doc.get("ingredient_count") is not None:
        parts.append(f"Ingredient count: {doc['ingredient_count']}")
    if doc.get("description"):
        parts.append(f"Description: {str(doc['description'])[:400]}")
    if doc.get("serves"):
        parts.append(f"Serves: {doc['serves']}")

    tags = [t for t in (doc.get("tags") or []) if not t.startswith(("source:", "type:"))]
    if tags:
        parts.append(f"Existing tags: {', '.join(tags[:15])}")

    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Candidate selection
# --------------------------------------------------------------------------- #
def candidate_query(*, double_tagged_only: bool, audit_all: bool) -> dict[str, Any]:
    """Recipes claiming ``main-dish`` that are worth asking about."""
    must_not = [{"terms": {"status": ["disabled", "deleted"]}}]
    filters: list[dict[str, Any]] = [{"term": {"course_types": "main-dish"}}]

    if double_tagged_only:
        filters.append({"term": {"course_types": "side"}})
        return {"bool": {"filter": filters, "must_not": must_not}}

    if audit_all:
        return {"bool": {"filter": filters, "must_not": must_not}}

    should: list[dict[str, Any]] = [
        {"match_phrase": {"title": word}} for word in ACCOMPANIMENT_WORDS
    ]
    # Already suspected by a previous writer, and thin recipes.
    should.append({"term": {"course_types": "side"}})
    should.append({"range": {"ingredient_count": {"lte": THIN_INGREDIENT_COUNT}}})
    return {
        "bool": {
            "filter": filters,
            "must_not": must_not,
            "should": should,
            "minimum_should_match": 1,
        }
    }


def fetch_candidates(entity, query: dict[str, Any], limit: int | None) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for hit in entity.scroll_all(query=query):
        docs.append(hit["_source"])
        if limit is not None and len(docs) >= limit:
            break
    return docs


# --------------------------------------------------------------------------- #
# Model pass
# --------------------------------------------------------------------------- #
def make_llm(*, backend: str, model: str, temperature: float, base_url: str):
    """Build the chat client once, so the pool does not rebuild it per recipe.

    ``local`` targets any OpenAI-compatible server (LM Studio, vLLM, Ollama),
    the same escape hatch ``scripts/neo4j/retag_dish_types_llm.py`` offers —
    useful when the Groq key is expired or the corpus pass would burn quota.
    """
    if backend == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=model, temperature=temperature)

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model, temperature=temperature, base_url=base_url, api_key="local"
    )


def call_model(prompt: str, *, llm, attempts: int = 4) -> dict[str, Any]:
    """One verdict, with backoff. Mirrors annotate_recipes.py's parsing."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = llm.invoke([("system", SYSTEM_PROMPT), ("human", prompt)])
            text = str(getattr(response, "content", response)).strip()
            if text.startswith("```"):
                text = text.split("```")[1].removeprefix("json").strip()
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(2**attempt)
    raise last if last else RuntimeError("model call failed")


def verdict_for(doc: dict[str, Any], *, llm) -> dict[str, Any]:
    """Return a validated verdict for one recipe.

    ``course_types`` is validated against the closed vocabulary and, when the
    model says the recipe is not a standalone main dish, ``main-dish`` is
    stripped from its proposal — a model that says "no" and then lists
    main-dish anyway has contradicted itself, and the explicit answer wins.
    """
    raw = call_model(build_user_prompt(doc), llm=llm)
    standalone = bool(raw.get("standalone_main_dish"))
    proposed = S.canonical_course_types(raw.get("course_types") or [])
    if not standalone:
        proposed = [c for c in proposed if c != "main-dish"]

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = None

    return {
        "standalone_main_dish": standalone,
        "course_types": proposed[:2],
        "reason": str(raw.get("reason") or "")[:400],
        "confidence": confidence,
    }


def resolve_change(
    doc: dict[str, Any], verdict: dict[str, Any]
) -> tuple[list[str], str] | None:
    """The new ``course_types``, or ``None`` to leave the recipe alone.

    Guards, in order:
      - a confirmed main dish is never touched, whatever else it proposed;
      - a rejection with no valid replacement is dropped, because writing an
        empty ``course_types`` would make the recipe unreachable by every
        planner slot instead of merely miscategorised;
      - a no-op change is reported as unchanged rather than written.
    """
    current = list(doc.get("course_types") or [])
    if verdict["standalone_main_dish"]:
        return None
    proposed = verdict["course_types"]
    if not proposed:
        return None
    if sorted(proposed) == sorted(current):
        return None
    return proposed, "demoted"


def apply_change(entity, doc: dict[str, Any], new_courses: list[str], verdict: dict[str, Any], *, run_id: str) -> None:
    """Write ``course_types`` plus matching evidence, atomically and reversibly.

    The document is re-read first so a concurrent annotation run's evidence for
    other facets is preserved — only ``course_types`` rows are replaced.
    """
    urn = doc.get("urn")
    fresh = entity.get(urn) or doc
    kept = [
        e for e in (fresh.get("annotation_evidence") or [])
        if e.get("facet") != "course_types"
    ]
    evidence = [
        V.evidence_entry(
            "course_types", value, method="model", confidence=verdict["confidence"]
        )
        for value in new_courses
    ]
    entity.enhance(
        urn,
        fields={"course_types": new_courses, "annotation_evidence": kept + evidence},
        agent=AGENT,
        run_id=run_id,
        refresh="false",
        reread=False,
    )


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write to Elasticsearch. Default is a dry run.")
    parser.add_argument("--recipe-id", default=None,
                        help="Audit a single recipe and exit.")
    parser.add_argument("--double-tagged-only", action="store_true",
                        help="Only recipes tagged both 'side' and 'main-dish' (57 docs).")
    parser.add_argument("--all", dest="audit_all", action="store_true",
                        help="Audit every main-dish recipe (~4.7k model calls).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after this many candidates.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent model calls (default 4). Groq rate limit is the ceiling.")
    parser.add_argument("--backend", choices=["groq", "local"], default="groq",
                        help="LLM backend (default groq).")
    parser.add_argument("--base-url", default="http://localhost:1234/v1",
                        help="OpenAI-compatible base URL for --backend local "
                             "(default LM Studio).")
    parser.add_argument("--model", default=None,
                        help=f"Model name. Default: groq -> {DEFAULT_MODEL}.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--min-confidence", type=float, default=0.0,
                        help="Skip demotions the model is less sure of than this.")
    parser.add_argument("--report", default="dumps/main_dish_audit.csv",
                        help="Review report path (CSV; a .jsonl sibling holds full verdicts).")
    parser.add_argument("--show-candidates", action="store_true",
                        help="List the candidate set and exit, without calling the model.")
    args = parser.parse_args()

    entity = recipe_entity()
    logger.info("Elasticsearch alias: %s", entity.alias)

    if args.recipe_id:
        doc = entity.get(args.recipe_id)
        if not doc:
            logger.error("recipe %s not found", args.recipe_id)
            sys.exit(1)
        targets = [doc]
    else:
        query = candidate_query(
            double_tagged_only=args.double_tagged_only, audit_all=args.audit_all
        )
        targets = fetch_candidates(entity, query, args.limit)

    logger.info("%d candidates claiming main-dish", len(targets))
    if not targets:
        return

    if args.show_candidates:
        for doc in targets:
            logger.info(
                "  %-12s %-24s %s",
                doc.get("recipe_id"), "+".join(doc.get("course_types") or []), doc.get("title"),
            )
        return

    run_id = f"main-dish-audit-{int(time.time())}"
    stats: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []

    model = args.model or DEFAULT_MODEL
    llm = make_llm(
        backend=args.backend, model=model,
        temperature=args.temperature, base_url=args.base_url,
    )
    logger.info("backend=%s model=%s", args.backend, model)

    def process(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, Exception | None]:
        try:
            return doc, verdict_for(doc, llm=llm), None
        except Exception as exc:  # noqa: BLE001
            return doc, None, exc

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for doc, verdict, error in pool.map(process, targets):
            done += 1
            if done % 100 == 0:
                logger.info("audited %d/%d", done, len(targets))

            if error is not None:
                stats["errors"] += 1
                if stats["errors"] <= 5:
                    logger.warning("verdict failed for %s: %s", doc.get("recipe_id"), error)
                continue

            current = list(doc.get("course_types") or [])
            change = resolve_change(doc, verdict)

            if change is None:
                stats["confirmed" if verdict["standalone_main_dish"] else "no_valid_replacement"] += 1
                continue

            new_courses, _ = change
            confidence = verdict["confidence"]
            if confidence is not None and confidence < args.min_confidence:
                stats["below_min_confidence"] += 1
                continue

            stats["demoted"] += 1
            rows.append({
                "recipe_id": doc.get("recipe_id"),
                "title": doc.get("title"),
                "source": doc.get("source"),
                "before": "+".join(current),
                "after": "+".join(new_courses),
                "confidence": confidence,
                "reason": verdict["reason"],
            })
            logger.info(
                "%s  %r  %s -> %s%s",
                doc.get("recipe_id"), doc.get("title"),
                current or "[]", new_courses,
                "" if args.apply else "  (dry run)",
            )

            if args.apply:
                try:
                    apply_change(entity, doc, new_courses, verdict, run_id=run_id)
                except Exception as exc:  # noqa: BLE001
                    stats["write_errors"] += 1
                    stats["demoted"] -= 1
                    logger.error("write failed for %s: %s", doc.get("recipe_id"), exc)

    if args.apply:
        # One refresh at the end; per-document waits cap throughput below 1/s.
        entity.es.refresh(entity.alias)

    # The report is the point of a dry run, so write it either way.
    if rows:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with report.with_suffix(".jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        logger.info("report: %s (+ .jsonl)", report)

    logger.info("--- summary (%s) ---", "applied" if args.apply else "dry run")
    logger.info("candidates          : %d", len(targets))
    for key in ("confirmed", "demoted", "no_valid_replacement",
                "below_min_confidence", "errors", "write_errors"):
        logger.info("%-20s: %d", key, stats[key])
    after = Counter(r["after"] for r in rows)
    if after:
        logger.info("demotion targets    : %s", dict(after.most_common()))


if __name__ == "__main__":
    main()
