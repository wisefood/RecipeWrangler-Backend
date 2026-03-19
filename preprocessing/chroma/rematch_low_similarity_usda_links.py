#!/usr/bin/env python3
"""Rematch low-similarity Recipe1M->USDA links using USDA nutritional Chroma collection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.get_embeddings import get_embeddings_batch


def parse_caret_fields(line: str) -> List[str]:
    return [field.strip().strip("~") for field in line.rstrip().split("^")]


def load_food_des(path: Path) -> Dict[str, str]:
    food_to_group: Dict[str, str] = {}
    with path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 2:
                continue
            food_id, group_id = fields[0], fields[1]
            if food_id and group_id:
                food_to_group[str(food_id)] = str(group_id)
    return food_to_group


def load_food_groups(path: Path) -> Dict[str, str]:
    group_map: Dict[str, str] = {}
    with path.open("r", encoding="latin-1", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            fields = parse_caret_fields(line)
            if len(fields) < 2:
                continue
            group_id, description = fields[0], fields[1]
            if group_id:
                group_map[str(group_id)] = str(description)
    return group_map


def _clean_text(text: object) -> str:
    return " ".join(str(text or "").strip().split())


def _distance_to_similarity(distance: object) -> float | None:
    if distance is None:
        return None
    try:
        return 1.0 - float(distance)
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rematch low-similarity links to better USDA candidates."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/mappings/recipe1m-usda-links-canonical.json"),
        help="Input links JSON path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (defaults to in-place).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.70,
        help="Rows with current embedding_similarity below this are rematched.",
    )
    parser.add_argument(
        "--min-new-similarity",
        type=float,
        default=0.70,
        help="Require new candidate similarity >= this to accept.",
    )
    parser.add_argument(
        "--min-improvement",
        type=float,
        default=0.00,
        help="Require new_similarity >= old_similarity + this.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of candidate USDA matches to fetch per row.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help="Batch size for embedding + Chroma query.",
    )
    parser.add_argument(
        "--collection",
        default="nutritional_ingredients_usda",
        help="USDA candidate Chroma collection.",
    )
    parser.add_argument(
        "--food-des",
        type=Path,
        default=Path("data/raw/usda/FOOD_DES.txt"),
        help="USDA FOOD_DES path for food group ids.",
    )
    parser.add_argument(
        "--fd-group",
        type=Path,
        default=Path("data/raw/usda/FD_GROUP.txt"),
        help="USDA FD_GROUP path for food group names.",
    )
    args = parser.parse_args()

    input_path = args.input
    output_path = args.output or args.input
    links = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(links, list):
        raise ValueError(f"Expected JSON array in {input_path}")

    food_to_group = load_food_des(args.food_des)
    group_map = load_food_groups(args.fd_group)

    low_indices: List[int] = []
    low_texts: List[str] = []
    for idx, row in enumerate(links):
        sim = row.get("embedding_similarity")
        if sim is None:
            continue
        try:
            sim_f = float(sim)
        except (TypeError, ValueError):
            continue
        if sim_f < args.threshold:
            canonical = _clean_text(row.get("canonical"))
            if canonical:
                low_indices.append(idx)
                low_texts.append(canonical)

    client = get_chroma_client()
    col = client.get_collection(name=args.collection)

    considered = len(low_indices)
    changed = 0
    no_candidate = 0
    below_new_floor = 0
    not_improved = 0

    for start in range(0, len(low_indices), args.batch_size):
        batch_indices = low_indices[start:start + args.batch_size]
        batch_texts = low_texts[start:start + args.batch_size]
        batch_vecs = get_embeddings_batch(batch_texts)
        results = col.query(
            query_embeddings=batch_vecs,
            n_results=max(1, int(args.top_k)),
            include=["documents", "metadatas", "distances"],
        )

        docs_all = results.get("documents") or []
        metas_all = results.get("metadatas") or []
        dists_all = results.get("distances") or []

        for local_i, row_idx in enumerate(batch_indices):
            row = links[row_idx]
            old_sim = float(row.get("embedding_similarity") or 0.0)
            old_usda_id = str(row.get("usda_id") or "")
            old_usda_label = str(row.get("usda_food_label") or "")

            docs = docs_all[local_i] if local_i < len(docs_all) else []
            metas = metas_all[local_i] if local_i < len(metas_all) else []
            dists = dists_all[local_i] if local_i < len(dists_all) else []

            best = None
            for doc, meta, dist in zip(docs, metas, dists):
                meta = meta or {}
                cand_sim = _distance_to_similarity(dist)
                if cand_sim is None:
                    continue
                usda_id = str(meta.get("usda_id") or "").strip()
                usda_label = _clean_text(meta.get("name") or doc or "")
                if not usda_id or not usda_label:
                    continue
                candidate = {
                    "usda_id": usda_id,
                    "usda_food_label": usda_label,
                    "similarity": cand_sim,
                }
                if best is None or candidate["similarity"] > best["similarity"]:
                    best = candidate

            if best is None:
                no_candidate += 1
                continue
            if best["similarity"] < float(args.min_new_similarity):
                below_new_floor += 1
                continue
            if best["similarity"] < old_sim + float(args.min_improvement):
                not_improved += 1
                continue

            new_usda_id = best["usda_id"]
            new_group_id = food_to_group.get(str(new_usda_id))
            new_group = group_map.get(str(new_group_id)) if new_group_id else None

            row["previous_usda_id"] = old_usda_id
            row["previous_usda_food_label"] = old_usda_label
            row["previous_embedding_similarity"] = old_sim

            row["usda_id"] = new_usda_id
            row["usda_food_label"] = best["usda_food_label"]
            row["food_group_id"] = str(new_group_id) if new_group_id else row.get("food_group_id")
            row["food_group"] = str(new_group) if new_group else row.get("food_group")
            row["embedding_similarity"] = float(best["similarity"])
            row["embedding_similarity_source"] = "chroma_rematch_nutritional_usda"

            row["rematch_changed"] = (new_usda_id != old_usda_id)
            changed += 1

    output_path.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"input_rows={len(links)}")
    print(f"considered_low_rows={considered}")
    print(f"accepted_updates={changed}")
    print(f"no_candidate={no_candidate}")
    print(f"below_new_floor={below_new_floor}")
    print(f"not_improved={not_improved}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()

