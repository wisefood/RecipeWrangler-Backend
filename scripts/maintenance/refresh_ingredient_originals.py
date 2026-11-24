#!/usr/bin/env python3
"""Make the graph's ``Ingredients_original`` lines match the raw scrape the parser read.

``sync_parsed_snapshot_to_graph.py`` links each parsed row to the original line at the raw-line position and never
checks the text. When the originals were loaded from an older scrape (FoodHero: an August file with glued lines) every
row after the first differing line points at another ingredient's text, and the recipe page shows that text. This script
compares originals and raw lines per recipe and, with ``--apply``, rewrites the original lines from the raw scrape (dropping stray
position links) and
rebuilds the ``MAPS_TO`` edges from ``HAS_INGREDIENT`` (parsed rows, quantities and weights are not touched).

Dry run by default. ``--apply`` writes a rollback file with the old originals and edges of every changed recipe.

  PYTHONPATH=src uv run --env-file .env python scripts/maintenance/refresh_ingredient_originals.py --report out.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from neo4j import GraphDatabase  # noqa: E402

from scripts.maintenance.sync_parsed_snapshot_to_graph import (  # noqa: E402
    DATASETS, _load_raw, _norm_url, misaligned, same_line,
)
from scripts.prepare_weight_ready_parser_snapshot import _title_key  # noqa: E402

READ = """
MATCH (r:Recipe {recipe_id: $rid})
OPTIONAL MATCH (r)-[h:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original)
RETURN h.position AS pos, coalesce(o.original_text, o.name) AS text ORDER BY pos
"""
READ_EDGES = """
MATCH (o:Ingredients_original)-[m:MAPS_TO]->(c:Ingredient) WHERE o.original_id STARTS WITH ($rid + ':')
RETURN o.original_id AS original_id, c.name AS name, properties(m) AS props
"""
WRITE = """
MATCH (r:Recipe {recipe_id: $rid})
CALL { WITH r MATCH (r)-[h:HAS_INGREDIENT_ORIGINAL]->() DELETE h }
CALL { WITH r MATCH (o:Ingredients_original) WHERE o.original_id STARTS WITH (r.recipe_id + ':')
       AND toInteger(split(o.original_id, ':')[-1]) >= size($lines) DETACH DELETE o }
CALL { WITH r MATCH (o:Ingredients_original) WHERE o.original_id STARTS WITH (r.recipe_id + ':')
       MATCH (o)-[m:MAPS_TO]->() DELETE m }
CALL { WITH r UNWIND range(0, size($lines) - 1) AS pos
  MERGE (o:Ingredients_original {original_id: r.recipe_id + ':' + toString(pos)})
    SET o.original_text = $lines[pos], o.name = $lines[pos], o.source = coalesce(o.source, r.source), o.status = 'active'
  MERGE (r)-[:HAS_INGREDIENT_ORIGINAL {position: pos}]->(o) }
CALL { WITH r MATCH (r)-[h:HAS_INGREDIENT]->(c) WHERE h.position < size($lines)
  MATCH (o:Ingredients_original {original_id: r.recipe_id + ':' + toString(h.position)})
  CREATE (o)-[m:MAPS_TO]->(c)
  SET m.measurement = h.measurement, m.unit = h.unit, m.weight_grams = h.weight_grams }
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="*", default=list(DATASETS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rollback", type=Path, help="required with --apply")
    args = parser.parse_args()
    if args.apply and not args.rollback:
        parser.error("--apply needs --rollback")

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )
    report: dict[str, list] = collections.defaultdict(list)
    rollback: dict[str, dict] = {}
    seen: set[str] = set()
    with driver.session() as session:
        for filename in args.files:
            source, url_key, title_key = DATASETS[filename]
            raw = _load_raw(filename, title_key)
            by_url: dict[str, list[str]] = collections.defaultdict(list)
            for rec in session.run("MATCH (r:Recipe {source: $s}) RETURN r.recipe_id AS id, r.url AS url", s=source):
                by_url[_norm_url(rec["url"])].append(rec["id"])
            for key, record in raw.items():
                ids = by_url.get(_norm_url(record.get(url_key)), [])
                if len(ids) != 1 or ids[0] in seen:
                    if len(ids) > 1:
                        report["ambiguous"].append({"source": source, "title": record.get("title") or record.get("name")})
                    continue
                rid = ids[0]
                seen.add(rid)
                raw_lines = [str(line) for line in record.get("ingredients") or []]
                rows = list(session.run(READ, rid=rid))
                originals = [r["text"] for r in rows if r["pos"] is not None]
                if not misaligned(originals, raw_lines):
                    continue
                item = {
                    "source": source, "recipe_id": rid, "title": record.get("title") or record.get("name"),
                    "originals": len(originals), "raw_lines": len(raw_lines),
                    "different": sum(not same_line(str(a or ""), b) for a, b in zip(originals, raw_lines)),
                }
                report["changed"].append(item)
                if args.apply:
                    rollback[rid] = {
                        "originals": [[r["pos"], r["text"]] for r in rows if r["pos"] is not None],
                        "edges": [dict(e) for e in session.run(READ_EDGES, rid=rid)],
                    }
                    args.rollback.write_text(json.dumps(rollback, ensure_ascii=False))
                    session.run(WRITE, rid=rid, lines=raw_lines).consume()
    args.report.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    by_source = collections.Counter(c["source"] for c in report["changed"])
    print(dict(by_source), "applied" if args.apply else "dry-run", {k: len(v) for k, v in report.items()})


if __name__ == "__main__":
    main()
