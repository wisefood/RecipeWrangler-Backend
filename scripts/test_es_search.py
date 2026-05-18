#!/usr/bin/env python3
"""Manual test harness for the Elasticsearch recipe search tool.

Two input modes:
  --question "..."   run the existing LLM constraint extractor, then search
  explicit flags     deterministic, no LLM (--include, --diet, --max-duration, ...)

The LLM (if used) runs once; the extracted constraints then drive BOTH the ES
search and the Neo4j param_search, so --compare isolates pure retrieval speed.
Each backend is run 3x: the first call is cold, later calls are warm.

Usage:
    PYTHONPATH=src python scripts/test_es_search.py --include chicken --max-duration 30 --compare
    PYTHONPATH=src python scripts/test_es_search.py --question "quick vegan dinner without nuts" --compare
    PYTHONPATH=src python scripts/test_es_search.py --dump-query --include rice tomato
"""

from __future__ import annotations

import argparse
import json
import time

from recipe_wrangler.tools.es_recipe_search import (
    RecipeSearchConstraints,
    build_es_query,
    search_recipes_es,
)

_RUNS = 3


def _print_results(results: list[dict]) -> None:
    if not results:
        print("  (no results)")
        return
    for i, r in enumerate(results, 1):
        flag = "★" if r.get("expert_recipe") else " "
        sust = r.get("sust_score")
        print(
            f"  {i:>2}. {flag} [{r.get('source','?'):<14}] "
            f"{(r.get('title') or '(untitled)')[:46]:<46} "
            f"dur={r.get('duration')} nutri={r.get('nutri_score')} "
            f"color={r.get('nutri_color')} sust={round(sust, 2) if sust is not None else None}"
        )


def _fmt_timing(label: str, times_ms: list[float], extra: str = "") -> str:
    cold = times_ms[0]
    warm = min(times_ms[1:]) if len(times_ms) > 1 else cold
    return f"{label}: cold={cold:.1f}ms  warm={warm:.1f}ms{extra}"


def _constraints_from_question(question: str) -> RecipeSearchConstraints:
    """Run the existing LLM extractor and map its output to ES constraints."""
    from recipe_wrangler.api.config import get_settings
    from recipe_wrangler.tools.text2cypher import RecipeSearchAppV2

    settings = get_settings()
    print(f"Extracting constraints via LLM ({settings.search_main_model})...")
    app = RecipeSearchAppV2(neo4j_uri=settings.neo4j_uri, model=settings.search_main_model)

    start = time.perf_counter()
    qc = app.run_extract_constraints(question)["query_constraints"]
    print(f"  LLM extraction took {(time.perf_counter() - start) * 1000:.0f}ms")
    print(f"  extracted: {json.dumps(qc, ensure_ascii=False)}")

    return RecipeSearchConstraints(
        include_ingredients=qc.get("preferred_ingredients", []),
        exclude_ingredients=qc.get("excluded_ingredients", []),
        exclude_allergens=qc.get("allergens", []),
        diet_tags=qc.get("diet", []),
        title_keywords=qc.get("title_keywords", []),
        max_duration_minutes=qc.get("max_duration_minutes"),
        min_servings=qc.get("min_servings"),
        limit=qc.get("limit", 10),
    )


def _run_es(c: RecipeSearchConstraints) -> None:
    times, took, last = [], [], None
    for _ in range(_RUNS):
        last = search_recipes_es(c)
        times.append(last["elapsed_ms"])
        took.append(last["es_took_ms"])
    print(
        "\n=== Elasticsearch ===\n"
        + _fmt_timing("  round-trip", times, f"  (ES took warm={min(took[1:] or took)}ms, "
        f"{last['total']} total hits)")
    )
    _print_results(last["results"])


def _run_compare(c: RecipeSearchConstraints) -> None:
    from recipe_wrangler.schemas import RecipeSearchFilters
    from recipe_wrangler.tools.param_search import search_recipes_by_params

    filters = RecipeSearchFilters(
        include_ingredients=c.include_ingredients,
        exclude_ingredients=c.exclude_ingredients,
        exclude_allergens=c.exclude_allergens,
        diet_tags=c.diet_tags,
        dish_types=c.dish_types,
        max_duration_minutes=c.max_duration_minutes,
        limit=c.limit,
        offset=c.offset,
    )
    times, rows = [], []
    for _ in range(_RUNS):
        start = time.perf_counter()
        rows = search_recipes_by_params(filters)
        times.append((time.perf_counter() - start) * 1000)

    print("\n=== Neo4j param_search ===\n" + _fmt_timing("  round-trip", times, f"  ({len(rows)} rows)"))
    _print_results(rows)
    if c.min_servings is not None or c.title_keywords:
        print("  note: Neo4j param_search ignores min_servings / title_keywords")


def main() -> None:
    p = argparse.ArgumentParser(description="Test the Elasticsearch recipe search.")
    p.add_argument("--question", default=None, help="natural-language query (runs LLM extractor)")
    p.add_argument("--include", nargs="*", default=[], help="must-have ingredients")
    p.add_argument("--exclude", nargs="*", default=[], help="excluded ingredients")
    p.add_argument("--allergens", nargs="*", default=[], help="excluded allergens")
    p.add_argument("--diet", nargs="*", default=[], help="required diet tags")
    p.add_argument("--dish-types", nargs="*", default=[], help="dish types (any match)")
    p.add_argument("--title", nargs="*", default=[], help="title keywords (relevance boost)")
    p.add_argument("--max-duration", type=int, default=None)
    p.add_argument("--min-servings", type=int, default=None)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--region", default="us", help="nutri-score region: us / ie / hu")
    p.add_argument("--dump-query", action="store_true", help="print the ES query body and exit")
    p.add_argument("--compare", action="store_true", help="also run Neo4j param_search")
    args = p.parse_args()

    if args.question:
        c = _constraints_from_question(args.question)
        c.limit, c.offset, c.region = args.limit, args.offset, args.region
    else:
        c = RecipeSearchConstraints(
            include_ingredients=args.include,
            exclude_ingredients=args.exclude,
            exclude_allergens=args.allergens,
            diet_tags=args.diet,
            dish_types=args.dish_types,
            title_keywords=args.title,
            max_duration_minutes=args.max_duration,
            min_servings=args.min_servings,
            limit=args.limit,
            offset=args.offset,
            region=args.region,
        )

    if args.dump_query:
        print(json.dumps(build_es_query(c), indent=2))
        return

    _run_es(c)
    if args.compare:
        _run_compare(c)


if __name__ == "__main__":
    main()
