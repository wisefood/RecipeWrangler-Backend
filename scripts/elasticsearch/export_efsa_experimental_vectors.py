#!/usr/bin/env python3
"""Export the EU composition table with experimental facets and embeddings."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from import_efsa_eu_fcdb import _load_foods  # noqa: E402
from recipe_wrangler.matching.facets import extract_candidate_facets  # noqa: E402
from recipe_wrangler.utils.get_embeddings import (  # noqa: E402
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_REVISION,
    get_embeddings_batch,
)
from recipe_wrangler.utils.nutrition_postgres import _env, get_engine  # noqa: E402


COLLECTION = "nutritional_ingredients_eu_efsa_experimental"
TABLE = _env("NUTRITION_EU_TABLE", "nutrients-ingredients-eu")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args()

    with get_engine().connect() as connection:
        rows = [dict(row) for row in connection.execute(text(
            f'''SELECT id, food_name, source, country, food_group,
                nutrients ?& ARRAY['Energy','Protein','Carbohydrate, by difference',
                    'Total lipid (fat)','Sugars, total','Fatty acids, total saturated',
                    'Sodium, Na','Fiber, total dietary'] AS nutrition_complete
                FROM "{TABLE}" ORDER BY id'''
        )).mappings()]
    foodex = {food["id"]: food for food in _load_foods().values()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    source_counts: Counter[str] = Counter()
    written = 0
    with args.output.open("wb") as output:
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start + args.batch_size]
            vectors = get_embeddings_batch([row["food_name"] for row in batch])
            for row, vector in zip(batch, vectors):
                extracted = extract_candidate_facets(row["food_name"])
                facets = extracted.facets
                extra = foodex.get(row["id"], {})
                record = {
                    "_source": {
                        "collection": COLLECTION,
                        "source_id": row["id"],
                        "document": row["food_name"],
                        "metadata": {
                            "eu_id": row["id"],
                            "food_name": row["food_name"],
                            "title": row["food_name"],
                            "source": row["source"],
                            "country": row["country"],
                            "food_group": row["food_group"] or "",
                            "foodex2_code": extra.get("foodex2_code") or "",
                            "foodex2_description": extra.get("foodex2_description") or "",
                            "identity": facets.identity or "",
                            "biological_source": facets.biological_source or "",
                            "food_part": facets.food_part or "",
                            "preparation_state": facets.preparation_state or "",
                            "processes": sorted(facets.processes),
                            "preservation_methods": sorted(facets.preservation_methods),
                            "physical_form": facets.physical_form or "",
                            "colour": facets.colour or "",
                            "sodium_level": facets.sodium_level or "",
                            "compound": facets.compound,
                            "facet_evidence": list(extracted.evidence),
                            "nutrition_complete": bool(row["nutrition_complete"]),
                            "experimental": True,
                        },
                        "embedding_model": DEFAULT_MODEL_NAME,
                        "embedding_revision": DEFAULT_MODEL_REVISION or "unspecified",
                        "vector_space": "cosine",
                        "embedding": vector,
                    }
                }
                line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                output.write(line)
                digest.update(line)
                source_counts[row["source"]] += 1
                written += 1
            print(f"progress embedded={written}/{len(rows)}", flush=True)

    manifest = {
        "total_count": written,
        "collections": {COLLECTION: written},
        "sources": dict(sorted(source_counts.items())),
        "embedding_model": DEFAULT_MODEL_NAME,
        "embedding_revision": DEFAULT_MODEL_REVISION,
        "sha256": digest.hexdigest(),
    }
    args.output.with_suffix(args.output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"done output={args.output} records={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
