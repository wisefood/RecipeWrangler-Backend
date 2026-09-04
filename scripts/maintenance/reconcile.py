#!/usr/bin/env python3
"""Detect and repair drift between the recipe owners and the search index.

Neo4j and Postgres own a recipe; Elasticsearch holds a projection of them plus
annotations no owner can reproduce. Every write is supposed to re-project. This
script is what makes "supposed to" checkable.

It answers four questions, in increasing cost:

1. **Is anything missing from the index?**  A recipe in Neo4j with no document
   is invisible to every reader. This has happened: a projection bug meant every
   recipe created after the read flip was silently absent from search, and
   nothing reported it because the write itself succeeded.

2. **Did any node change after its last projection?**  Comparing the digest
   re-derived from the node against the `content_digest` stamped on it during
   projection. No Elasticsearch read required.

3. **Did any projection fail to land?**  Comparing the digest stamped on the
   node against the one stored in the document.

4. **What actually differs?**  A field-by-field comparison for the recipes the
   cheaper checks flagged, so the report says "titles differ" rather than
   "hashes differ".

Plus two checks that do not use digests: **orphans** (a document whose recipe no
longer exists in Neo4j — a reader can still find it and every link 404s) and
**status counts** per state, deliberately excluded from the digest because
disable/enable sync through their own bulk path.

Dry-run by default. `--apply` re-projects the recipes that need it; nothing is
ever deleted without `--delete-orphans`, which is separate on purpose.

Usage
-----
  # Report only (default)
  python scripts/maintenance/reconcile.py

  # Repair by re-projecting everything that drifted or is missing
  python scripts/maintenance/reconcile.py --apply

  # Faster pass on a big corpus: skip the field-level diff
  python scripts/maintenance/reconcile.py --no-explain

  # Check one source, or one recipe
  python scripts/maintenance/reconcile.py --source myplate
  python scripts/maintenance/reconcile.py --recipe-id abc123 --explain

  # Remove documents whose recipe is gone from Neo4j
  python scripts/maintenance/reconcile.py --delete-orphans --apply

Exit codes
----------
0  everything consistent (or repaired with --apply)
1  drift found and not repaired — suitable for a cron/CI gate
2  the run itself failed
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

sys.path.insert(0, "src")

logger = logging.getLogger("reconcile")

@dataclass
class Report:
    """What the run found. Counts are cheap; id lists drive the repair."""

    checked: int = 0
    missing: list[str] = field(default_factory=list)
    stale_owner: list[str] = field(default_factory=list)
    stale_index: list[str] = field(default_factory=list)
    unstamped: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    # Set by `catalog.writer.commit` when a step did not complete. A recipe can
    # be perfectly consistent between the stores and still be unfinished — an
    # annotation outage leaves the document correct and the recipe
    # undiscoverable, which no digest comparison can see.
    annotation_pending: list[str] = field(default_factory=list)
    # Recipes whose last commit recorded a projection failure. Kept separate
    # from `missing` so the report does not claim a document is absent when the
    # commit simply gave up partway; both still need re-projecting.
    commit_failed: list[str] = field(default_factory=list)
    explained: dict[str, list[str]] = field(default_factory=dict)
    repaired: int = 0
    repair_failures: list[tuple[str, str]] = field(default_factory=list)
    deleted_orphans: int = 0

    @property
    def needs_projection(self) -> list[str]:
        """Every recipe whose document is absent or out of date.

        Deduplicated and ordered, because a single recipe can be flagged by
        more than one check and re-projecting it twice is wasted work.
        """
        seen: dict[str, None] = {}
        for group in (
            self.missing,
            self.commit_failed,
            self.stale_owner,
            self.stale_index,
        ):
            for recipe_id in group:
                seen.setdefault(recipe_id, None)
        return list(seen)

    @property
    def clean(self) -> bool:
        return not self.needs_projection and not self.orphans


# Everything the digest needs, straight from the owner. Deliberately a
# different, much cheaper query than `projection.RECIPE_QUERY`: reconciliation
# reads the whole corpus, so pulling the ingredient class ancestry and allergen
# evidence for every recipe would make the check too slow to run often — and a
# check that is too slow to run is a check that does not run.
OWNER_QUERY = """
MATCH (r:Recipe)
WHERE ($sources IS NULL OR r.source IN $sources)
  AND ($rid IS NULL OR r.recipe_id = $rid OR r.id = $rid)
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
  RETURN collect(DISTINCT i.name) AS ingredients
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
  RETURN collect(DISTINCT a.name) AS allergens
}
CALL (r) {
  OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
  RETURN collect(DISTINCT t.name) AS tags,
         collect(DISTINCT CASE WHEN t.category IN ['dietary','dietary_option']
                          THEN t.name END) AS diet_tags
}
CALL (r) {
  OPTIONAL MATCH (r)-[:SUITABLE_FOR]->(g:ConsumerGroup)
  RETURN collect(DISTINCT g.name) AS suitable_for
}
RETURN coalesce(r.recipe_id, r.id) AS recipe_id,
       r.title AS title,
       r.description AS description,
       r.instructions AS instructions,
       r.url AS url,
       r.image_url AS image_url,
       r.source AS source,
       r.source_id AS source_id,
       r.duration AS duration,
       r.serves AS serves,
       r.cost_category AS cost_category,
       r.cost_category_code AS cost_category_code,
       r.cost_category_status AS cost_category_status,
       r.cost_price_coverage AS cost_price_coverage,
       r.expert_recipe AS expert_recipe,
       r.content_digest AS stamped_digest,
       coalesce(r.projection_pending, false) AS projection_pending,
       coalesce(r.annotation_pending, false) AS annotation_pending,
       ingredients, allergens, tags, diet_tags, suitable_for
ORDER BY recipe_id
"""


def _clean_text(value: Any) -> Any:
    """Neo4j returns list-valued text for some fields; the projection joins it."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(part) for part in value if part is not None)
    return value


def owner_document(row: dict[str, Any]) -> dict[str, Any]:
    """Shape an owner row into what the digest expects.

    Every field in `integrity.DIGESTED_FIELDS` must be produced here, or the
    owner digest and the stored one disagree for every recipe that has the
    missing field — and a checker that reports the whole corpus as drifted is
    one nobody reads. `test_reconcile_covers_every_digested_field` pins that.

    This bit us once already: `diet_tags` and `suitable_for` were in the digest
    and not in this query. It went unnoticed only because no digests had been
    stamped yet, so the comparison was being skipped entirely.
    """
    return {
        "recipe_id": row.get("recipe_id"),
        "title": _clean_text(row.get("title")),
        "description": _clean_text(row.get("description")),
        "instructions": _clean_text(row.get("instructions")),
        "url": row.get("url"),
        "image_url": row.get("image_url"),
        "source": row.get("source"),
        "source_id": row.get("source_id"),
        "duration": row.get("duration"),
        "serves": row.get("serves"),
        "cost_category": row.get("cost_category"),
        "cost_category_code": row.get("cost_category_code"),
        "cost_category_status": row.get("cost_category_status"),
        "cost_price_coverage": row.get("cost_price_coverage"),
        "expert_recipe": bool(row.get("expert_recipe")),
        "ingredients": row.get("ingredients") or [],
        "allergens": row.get("allergens") or [],
        "tags": row.get("tags") or [],
        "diet_tags": row.get("diet_tags") or [],
        "suitable_for": row.get("suitable_for") or [],
    }


def resolve_source(slug: str | None) -> list[str] | None:
    """Turn a source slug into the raw names Neo4j actually stores.

    Neo4j holds display names — `FoodHero`, `Curated Irish Recipes` — while
    Elasticsearch and every CLI flag use slugs. Passing the slug straight into
    Cypher matches nothing and the run reports a clean zero, which is the worst
    possible answer from a consistency checker: it looks like success.
    """
    if not slug:
        return None
    from recipe_wrangler.catalog import sources as S

    raw = S.raw_for(slug)
    if not raw:
        raise SystemExit(
            f"unknown source {slug!r} — known slugs: "
            + ", ".join(sorted(src.slug for src in S.SOURCES))
        )
    return [raw]


def fetch_owner_rows(
    sources: list[str] | None, recipe_id: str | None
) -> list[dict[str, Any]]:
    from recipe_wrangler.utils.neo4j_utils import driver

    with driver.session() as session:
        return session.run(
            OWNER_QUERY, {"sources": sources, "rid": recipe_id}
        ).data()


def scan_index(sources: list[str] | None) -> dict[str, dict[str, Any]]:
    """Every document's digest and status, in one pass.

    A single scan answers both questions the run needs: which owners have no
    document (ids present in Neo4j and absent here), and which documents have
    no owner (the reverse). Two separate scans would double the work and could
    disagree with each other if a write landed between them.

    `scroll_all` uses search_after, so this is not subject to the 10,000-result
    window that `from`/`size` would hit at ~1.4% of this corpus.
    """
    from recipe_wrangler.catalog.entities import recipe_entity

    entity = recipe_entity()
    query = {"terms": {"source": sources}} if sources else {"match_all": {}}

    found: dict[str, dict[str, Any]] = {}
    for hit in entity.es.scroll_all(
        entity.alias,
        query=query,
        source=["recipe_id", "content_digest", "status"],
        page_size=1000,
    ):
        doc = hit.get("_source") or hit
        rid = doc.get("recipe_id")
        if rid:
            found[str(rid)] = doc
    return found


def explain(recipe_ids: Iterable[str], limit: int) -> dict[str, list[str]]:
    """Which fields differ, for up to `limit` recipes.

    Rebuilds each document through the real projection rather than the cheap
    reconciliation row, so the comparison is against what *would* be written —
    otherwise the report would blame fields that only the full query produces.
    """
    from recipe_wrangler.catalog.entities import recipe_entity
    from recipe_wrangler.catalog.integrity import digest_differences
    from recipe_wrangler.catalog.projection import build_document, fetch_owner_row

    entity = recipe_entity()
    out: dict[str, list[str]] = {}

    for recipe_id in list(recipe_ids)[:limit]:
        try:
            row = fetch_owner_row(recipe_id)
            if row is None:
                out[recipe_id] = ["<missing from Neo4j>"]
                continue
            # Validated, not raw: `Recipe.validate` canonicalises course types
            # and reshapes ingredients before indexing, so an unvalidated
            # document is not what the index would receive. Comparing against
            # it reports differences that a re-projection would not fix.
            expected = entity.validate(dict(build_document(row)))
            actual = entity.get(recipe_id)
            if not actual:
                out[recipe_id] = ["<no document>"]
                continue
            out[recipe_id] = digest_differences(expected, actual) or ["<digest only>"]
        except Exception as exc:  # noqa: BLE001
            out[recipe_id] = [f"<comparison failed: {exc}>"]
    return out


def status_counts() -> tuple[dict[str, int], dict[str, int]]:
    """Status distribution on each side.

    Status is excluded from the digest — disable/enable sync through their own
    bulk path — so it is verified by counting instead. Equal counts do not prove
    the same recipes are disabled on both sides, only that no bulk job half-ran;
    that is the failure this is actually guarding against.
    """
    from recipe_wrangler.catalog.entities import recipe_entity
    from recipe_wrangler.utils.neo4j_utils import driver

    with driver.session() as session:
        rows = session.run(
            "MATCH (r:Recipe) RETURN coalesce(r.status, 'active') AS status, "
            "count(*) AS n"
        ).data()
    owners = {str(r["status"]): int(r["n"]) for r in rows}

    entity = recipe_entity()
    response = entity.es.search(
        entity.alias,
        {"size": 0, "aggs": {"status": {"terms": {"field": "status", "size": 20}}}},
    )
    buckets = response.get("aggregations", {}).get("status", {}).get("buckets", [])
    index = {str(b["key"]): int(b["doc_count"]) for b in buckets}
    return owners, index


def reconcile(
    *,
    source: str | None,
    recipe_id: str | None,
    do_explain: bool,
    explain_limit: int,
) -> Report:
    from recipe_wrangler.catalog.integrity import content_digest

    report = Report()

    sources = resolve_source(source)
    logger.info(
        "reading owners from Neo4j%s",
        f" (source={source} -> {sources})" if sources else "",
    )
    rows = fetch_owner_rows(sources, recipe_id)
    report.checked = len(rows)
    if not rows:
        logger.warning("no recipes matched — nothing to check")
        return report

    owner_digests: dict[str, str] = {}
    for row in rows:
        rid = str(row.get("recipe_id") or "").strip()
        if not rid:
            continue
        derived = content_digest(owner_document(row))
        owner_digests[rid] = derived

        if row.get("projection_pending"):
            # The commit path already knows this one failed. Trusting the marker
            # rather than re-deriving it means a recipe whose projection threw
            # is repaired even if its digest happens to look right.
            report.commit_failed.append(rid)
        if row.get("annotation_pending"):
            report.annotation_pending.append(rid)

        stamped = row.get("stamped_digest")
        if not stamped:
            report.unstamped.append(rid)
        elif str(stamped) != derived:
            # The node changed after its last projection, by a path that did
            # not re-project. The index is serving a stale answer right now.
            report.stale_owner.append(rid)

    logger.info("scanning the index")
    indexed = scan_index(sources)

    for rid in owner_digests:
        doc = indexed.get(rid)
        if doc is None:
            report.missing.append(rid)
            continue
        stored = doc.get("content_digest")
        if stored and str(stored) != owner_digests[rid] and rid not in report.stale_owner:
            # Node and document disagree, and the node matches its own stamp —
            # so the projection is what did not land.
            report.stale_index.append(rid)

    # Orphans fall out of the same scan: a document whose recipe_id is not in
    # the owner set. Safe under --source because the scan was filtered to the
    # same source; not safe under --recipe-id, where the owner set is a single
    # id and every other document would look orphaned.
    if recipe_id is None:
        report.orphans = [rid for rid in indexed if rid not in owner_digests]
    else:
        logger.info("skipping orphan detection (single-recipe run)")

    if do_explain and report.needs_projection:
        logger.info("explaining up to %d differences", explain_limit)
        report.explained = explain(report.needs_projection, explain_limit)

    return report


def repair(report: Report, *, delete_orphans: bool) -> None:
    from recipe_wrangler.catalog.entities import recipe_entity
    from recipe_wrangler.catalog.projection import ProjectionError, project

    targets = report.needs_projection
    if targets:
        logger.info("re-projecting %d recipes", len(targets))
        for recipe_id in targets:
            try:
                project(recipe_id, refresh="false")
                report.repaired += 1
            except ProjectionError as exc:
                report.repair_failures.append((recipe_id, str(exc)))
            except Exception as exc:  # noqa: BLE001
                report.repair_failures.append((recipe_id, repr(exc)))
        entity = recipe_entity()
        entity.es.refresh(entity.alias)

    if delete_orphans and report.orphans:
        entity = recipe_entity()
        logger.info("deleting %d orphaned documents", len(report.orphans))
        for recipe_id in report.orphans:
            try:
                entity.es.delete(entity.alias, f"urn:recipe:{recipe_id}")
                report.deleted_orphans += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete orphan %s: %s", recipe_id, exc)


def print_report(report: Report, *, applied: bool) -> None:
    def line(label: str, values: list[str]) -> None:
        mark = "  " if not values else "! "
        print(f"{mark}{label:<34} {len(values):>7}")
        for value in values[:5]:
            print(f"      {value}")
        if len(values) > 5:
            print(f"      … and {len(values) - 5} more")

    print()
    print("=" * 62)
    print(f"  recipes checked{'':<19}{report.checked:>7}")
    print("-" * 62)
    line("missing from index", report.missing)
    line("owners changed since projection", report.stale_owner)
    line("projection did not land", report.stale_index)
    line("orphaned documents", report.orphans)
    line("never stamped (pre-digest)", report.unstamped)
    line("commit recorded a failure", report.commit_failed)
    line("annotation pending", report.annotation_pending)
    print("=" * 62)

    if report.explained:
        print("\n  what differs:")
        for recipe_id, fields in report.explained.items():
            print(f"    {recipe_id}: {', '.join(fields)}")

    if applied:
        print(f"\n  re-projected {report.repaired}")
        if report.deleted_orphans:
            print(f"  deleted orphans {report.deleted_orphans}")
        for recipe_id, error in report.repair_failures[:10]:
            print(f"  FAILED {recipe_id}: {error}")
        if len(report.repair_failures) > 10:
            print(f"  … and {len(report.repair_failures) - 10} more failures")
    elif not report.clean:
        print("\n  dry run — re-run with --apply to repair")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect and repair drift between recipe owners and the search index.",
    )
    parser.add_argument("--source", help="Restrict to one source slug.")
    parser.add_argument("--recipe-id", help="Check a single recipe.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Re-project what drifted. Without this the run only reports.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Also delete documents whose recipe is gone from Neo4j. Needs --apply.",
    )
    parser.add_argument(
        "--explain",
        dest="explain",
        action="store_true",
        default=True,
        help="Report which fields differ (default).",
    )
    parser.add_argument(
        "--no-explain",
        dest="explain",
        action="store_false",
        help="Skip the field-level comparison — faster on a large corpus.",
    )
    parser.add_argument(
        "--explain-limit",
        type=int,
        default=20,
        help="How many recipes to explain in detail (default 20).",
    )
    parser.add_argument(
        "--status-check",
        action="store_true",
        help="Also compare status counts between the stores.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    try:
        report = reconcile(
            source=args.source,
            recipe_id=args.recipe_id,
            do_explain=args.explain,
            explain_limit=args.explain_limit,
        )

        if args.apply:
            repair(report, delete_orphans=args.delete_orphans)
        elif args.delete_orphans:
            logger.warning("--delete-orphans has no effect without --apply")

        print_report(report, applied=args.apply)

        if args.status_check:
            owners, index = status_counts()
            print("  status counts")
            for state in sorted(set(owners) | set(index)):
                a, b = owners.get(state, 0), index.get(state, 0)
                mark = "  " if a == b else "! "
                print(f"  {mark}{state:<20} owners={a:<8} index={b}")
            print()

        if args.apply:
            return 1 if report.repair_failures else 0
        return 0 if report.clean else 1
    except Exception:  # noqa: BLE001
        logger.exception("reconciliation failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
