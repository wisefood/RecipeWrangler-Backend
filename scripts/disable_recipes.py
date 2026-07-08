"""Bulk disable/enable (soft-delete) recipes — the corpus-scale tool.

Neo4j `r.status` is the source of truth; ES (recipes_v2 + the legacy index)
is synced per batch right after each Neo4j batch commits. Missing status
means active, so the operation is safe to re-run until converged.

Usage
-----
  # Dry-run (default): report what would be disabled
  python scripts/disable_recipes.py --source recipe1m

  # Disable a whole source with a reason
  python scripts/disable_recipes.py --source recipe1m \
      --reason "quality: pending retagging" --apply

  # Disable explicit IDs (inline or one-per-line file)
  python scripts/disable_recipes.py --ids id1,id2,id3 --apply
  python scripts/disable_recipes.py --ids-file bad_ids.txt --apply

  # Re-enable
  python scripts/disable_recipes.py --source recipe1m --enable --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()

from recipe_wrangler.repositories.neo4j_recipes import (
    resolve_recipe_ids_by_query,
    set_recipe_status,
)
from recipe_wrangler.schemas import RecipeSearchFilters
from recipe_wrangler.tools.param_search import _build_where_clause, _has_no_constraints
from recipe_wrangler.utils.recipe_status import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    sync_recipe_status_to_es,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 5000


def _collect_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return [i.strip() for i in args.ids.split(",") if i.strip()]
    if args.ids_file:
        return [
            line.strip()
            for line in Path(args.ids_file).read_text().splitlines()
            if line.strip()
        ]

    filters = RecipeSearchFilters(
        sources=[args.source] if args.source else [],
        dish_types=[args.dish_type] if args.dish_type else [],
        diet_tags=[args.diet_tag] if args.diet_tag else [],
        # Enabling must be able to see the disabled recipes it targets.
        include_disabled=args.enable or args.include_disabled,
    )
    if _has_no_constraints(filters) and not args.all:
        logger.error(
            "No filter given — refusing to target the whole corpus. "
            "Pass --all if that is really intended."
        )
        sys.exit(2)
    where_clause, params = _build_where_clause(filters)
    logger.info("Resolving matching recipe IDs from Neo4j (paged)...")
    return resolve_recipe_ids_by_query(where_clause, params)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ids", help="Comma-separated recipe IDs")
    parser.add_argument("--ids-file", help="File with one recipe ID per line")
    parser.add_argument("--source", help="Target every recipe of this source (canonical slug)")
    parser.add_argument("--dish-type", help="Target every recipe with this dish-type tag")
    parser.add_argument("--diet-tag", help="Target every recipe with this diet tag")
    parser.add_argument("--all", action="store_true",
                        help="Allow an unfiltered (whole-corpus) operation")
    parser.add_argument("--enable", action="store_true",
                        help="Re-enable instead of disable")
    parser.add_argument("--include-disabled", action="store_true",
                        help="Match already-disabled recipes too when filtering")
    parser.add_argument("--reason", help="Stored as r.disabled_reason (disable only)")
    parser.add_argument("--skip-es", action="store_true",
                        help="Only update Neo4j; run again later or reindex to converge ES")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Default is dry-run.")
    args = parser.parse_args()

    from recipe_wrangler.api.config import get_settings
    from recipe_wrangler.tools.es_recipe_search import ES_INDEX

    status = STATUS_ACTIVE if args.enable else STATUS_DISABLED
    ids = _collect_ids(args)
    logger.info("Matched %d recipe(s); target status='%s'", len(ids), status)
    if not ids:
        return
    if not args.apply:
        logger.info("Dry-run only (sample: %s). Re-run with --apply to write.", ids[:10])
        return

    settings = get_settings()
    indices = list(dict.fromkeys([ES_INDEX, settings.elastic_index]))
    totals = {"neo4j": 0, "es": {i: {"updated": 0, "not_found": 0, "errors": 0} for i in indices}}
    t0 = time.monotonic()

    # Neo4j batch first, ES sync right after — a crash mid-way leaves prior
    # batches fully converged and the rest untouched; re-running converges all.
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start + BATCH_SIZE]
        updated = set_recipe_status(batch, status, args.reason)
        totals["neo4j"] += len(updated)
        if updated and not args.skip_es:
            stats = sync_recipe_status_to_es(
                updated, status, es_url=settings.elastic_url, indices=indices
            )
            for index, s in stats.items():
                for k, v in s.items():
                    totals["es"][index][k] += v
        logger.info(
            "Progress: %d/%d neo4j-updated=%d (%.1fs)",
            min(start + BATCH_SIZE, len(ids)), len(ids), totals["neo4j"],
            time.monotonic() - t0,
        )

    logger.info("=== Done in %.1fs ===", time.monotonic() - t0)
    logger.info("Matched   : %d", len(ids))
    logger.info("Neo4j set : %d (unmatched IDs: %d)", totals["neo4j"], len(ids) - totals["neo4j"])
    for index, s in totals["es"].items():
        logger.info("ES %-12s: %s", index, s)
    logger.info(
        "Note: cached API responses expire with the Redis TTL; "
        "the API endpoints purge them immediately, this CLI does not."
    )


if __name__ == "__main__":
    main()
