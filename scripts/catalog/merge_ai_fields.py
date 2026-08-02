#!/usr/bin/env python3
"""Fold ``ai_<facet>`` values into ``<facet>`` and drop the twin.

One-off migration for the decision to stop keeping a parallel ``ai_`` field per
discovery facet. Provenance does not depend on the field name: every value has
an ``annotation_evidence`` entry recording method and confidence, and
``enhancements[].before`` keeps whatever a model replaced.

``ai_allergens`` is deliberately NOT merged. A model-guessed allergen shown
indistinguishably from a declared one is a safety failure, so that twin stays.

Runs as an ``_update_by_query`` painless script, so it never leaves the cluster.

Usage
-----
  python scripts/catalog/merge_ai_fields.py            # report only
  python scripts/catalog/merge_ai_fields.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from recipe_wrangler.catalog.elastic import get_catalog_client
from recipe_wrangler.catalog.entities import recipe_entity

logger = logging.getLogger("merge_ai_fields")

# ai_allergens is absent on purpose — see module docstring.
MERGE_FACETS = ("course_types", "cuisines", "flavor_profiles", "moods", "food_groups")

# The AI value wins where both exist: for these facets the stored value is a
# scraped source tag (79.8% of course types were "main-dish", cakes included),
# not a curated one. The replaced value stays in enhancements[].before.
SCRIPT = """
for (String facet : params.facets) {
  String aiField = 'ai_' + facet;
  if (ctx._source.containsKey(aiField)) {
    def aiValue = ctx._source.remove(aiField);
    if (aiValue != null && !(aiValue instanceof List && aiValue.isEmpty())) {
      ctx._source[facet] = aiValue;
    }
  }
}
if (ctx._source.ai_generated_fields != null) {
  def kept = [];
  for (f in ctx._source.ai_generated_fields) {
    if (f != null && f.startsWith('ai_')) {
      String bare = f.substring(3);
      if (params.facets.contains(bare)) { kept.add(bare); }
      else { kept.add(f); }
    } else if (f != null) { kept.add(f); }
  }
  ctx._source.ai_generated_fields = kept;
}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--alias", default=None)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
    )

    client = get_catalog_client()
    alias = args.alias or recipe_entity().alias

    should = [{"exists": {"field": f"ai_{f}"}} for f in MERGE_FACETS]
    query = {"bool": {"should": should, "minimum_should_match": 1}}

    logger.info("index=%s facets=%s", alias, ", ".join(MERGE_FACETS))
    for facet in MERGE_FACETS:
        ai = client.count(alias, {"exists": {"field": f"ai_{facet}"}})
        plain = client.count(alias, {"exists": {"field": facet}})
        logger.info("  %-18s ai=%-6s plain=%-6s", facet, ai, plain)

    affected = client.count(alias, query)
    logger.info("documents to rewrite: %s", affected)

    if not args.apply:
        logger.info("no --apply — nothing written.")
        return

    result = client._request(
        "POST",
        f"{alias}/_update_by_query",
        params={"conflicts": "proceed", "refresh": "true", "wait_for_completion": "true"},
        body={"query": query, "script": {"lang": "painless", "source": SCRIPT,
                                         "params": {"facets": list(MERGE_FACETS)}}},
        timeout=1800.0,
    )
    logger.info(
        "updated=%s failures=%s", result.get("updated"), len(result.get("failures") or [])
    )
    if result.get("failures"):
        logger.error("%s", json.dumps(result["failures"][:2])[:600])

    for facet in MERGE_FACETS:
        ai = client.count(alias, {"exists": {"field": f"ai_{facet}"}})
        plain = client.count(alias, {"exists": {"field": facet}})
        logger.info("  %-18s ai=%-6s plain=%-6s", facet, ai, plain)


if __name__ == "__main__":
    main()
