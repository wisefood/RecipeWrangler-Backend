#!/usr/bin/env python3
"""Split FoodHero compound ingredient nodes via an LLM.

After the regex normaliser there are ~720 FoodHero Ingredient nodes whose names
are not atomic ingredients — e.g. ``"margarine or butter"``, ``"vegetable oil½
medium onion"``, ``"drained and rinsed1 can great northern bean"``. Each can:

1. carry multiple atomic ingredients glued together by FoodHero's broken scrape;
2. be a pure-modifier leftover (``"drained and rinsed"``, ``"cooked and
   crumbled"``) which we just drop;
3. be ambiguous — handled as case 1 with the model's best guess.

We batch 20 compound names per Groq prompt and ask for a JSON dict mapping
each input to a list of atomic ingredients (or an empty list to drop). The
new edges are then written into Neo4j and the old compound node becomes an
orphan + gets deleted.

Idempotent at the recipe level — re-running won't duplicate edges because the
old compound rel is deleted as part of each split.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from groq import Groq  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402

DEFAULT_BATCH = 20
DEFAULT_MODEL = "llama-3.3-70b-versatile"
CKPT = REPO_ROOT / "data_to_send" / "migration" / "foodhero_split_checkpoint.json"

CANDIDATES_CYPHER = """
MATCH (rec:Recipe {source:'FoodHero'})-[:HAS_INGREDIENT]->(i:Ingredient)
WHERE size(i.name) > 30
   OR i.name CONTAINS ' or '
   OR i.name CONTAINS ' and '
   OR i.name CONTAINS 'each'
   OR i.name =~ '.*[½¼¾⅓⅔⅛⅜⅝⅞].*'
WITH i.name AS name, count(DISTINCT rec) AS uses
RETURN name, uses
ORDER BY uses DESC, name
"""

APPLY_CYPHER = """
UNWIND $rows AS row
CALL (row) {
    MATCH (old:Ingredient {name: row.raw_name})
    WITH old, row LIMIT 1
    UNWIND row.atomic AS atomic_name
    MERGE (clean:Ingredient {name: atomic_name})
        ON CREATE SET clean.canonical_id = randomUUID()
    WITH old, clean
    MATCH (rec:Recipe {source:'FoodHero'})-[h:HAS_INGREDIENT]->(old)
    WITH old, clean, rec, h, properties(h) AS props
    MERGE (rec)-[h2:HAS_INGREDIENT]->(clean)
        ON CREATE SET h2 = props
} IN TRANSACTIONS OF 200 ROWS
"""

DROP_OLD_CYPHER = """
UNWIND $names AS n
MATCH (rec:Recipe {source:'FoodHero'})-[h:HAS_INGREDIENT]->(:Ingredient {name: n})
DELETE h
"""

DROP_ORPHANS = """
MATCH (i:Ingredient)
WHERE NOT (:Recipe)-[:HAS_INGREDIENT]->(i)
  AND NOT (i)<-[:HAS_INGREDIENT_ORIGINAL]-()
  AND NOT (i)-[:HAS_ALLERGEN]-()
  AND NOT (i)-[:HAS_CLASS]-()
  AND NOT (i)-[:HAS_SUBSTITUTION]-()
  AND NOT (i)-[:FLAVORDB_EQUIVALENT]-()
WITH i LIMIT 5000
DETACH DELETE i
RETURN count(*) AS deleted
"""

PROMPT_SYSTEM = (
    "You split garbled recipe ingredient lines into atomic ingredients. "
    "Each input came from a broken scraper — it may be one ingredient, multiple "
    "ingredients concatenated with no separator (e.g. 'vegetable oil½ medium onion'), "
    "alternatives joined by 'or' (e.g. 'margarine or butter' → ['margarine','butter']), "
    "or a modifier-only fragment (e.g. 'drained and rinsed','frozen or canned') which "
    "should map to an empty list. Return ONLY canonical short ingredient nouns, "
    "lowercase, singular when natural, no quantities, no units, no parentheticals. "
    "Examples: 'lemon juice', 'olive oil', 'black pepper', 'onion'."
)

PROMPT_TEMPLATE = (
    "Split each input into atomic ingredients. Reply with ONLY a JSON object "
    "(no prose, no markdown fences). Keys are the input strings exactly as given. "
    "Values are JSON arrays of canonical short ingredient names — empty array if "
    "the input is purely a modifier with no real ingredient.\n\n"
    "Inputs:\n{inputs}"
)


def _batches(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _call_groq(client: Groq, model: str, items: list[str]) -> dict[str, list[str]]:
    payload = "\n".join(f"- {x!r}" for x in items)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user", "content": PROMPT_TEMPLATE.format(inputs=payload)},
        ],
    )
    txt = resp.choices[0].message.content
    parsed = json.loads(txt)
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in parsed.items():
        if isinstance(v, list):
            out[k] = [str(x).strip().lower() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return out


def _load_ckpt() -> dict[str, list[str]]:
    if not CKPT.exists():
        return {}
    try:
        return json.loads(CKPT.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_ckpt(d: dict) -> None:
    CKPT.parent.mkdir(parents=True, exist_ok=True)
    CKPT.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--write", action="store_true")
    p.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    drv = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    )
    try:
        with drv.session() as s:
            candidates = [(r["name"], r["uses"]) for r in s.run(CANDIDATES_CYPHER)]
    finally:
        pass
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"FoodHero compound candidates: {len(candidates)}")

    plan = _load_ckpt()
    todo = [name for name, _ in candidates if name not in plan]
    print(f"already in checkpoint: {len(plan)}; remaining to query LLM: {len(todo)}")

    if todo:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        t0 = time.time()
        for idx, batch in enumerate(_batches(todo, args.batch)):
            try:
                result = _call_groq(client, args.model, batch)
            except Exception as exc:  # noqa: BLE001
                print(f"  batch {idx} FAILED ({type(exc).__name__}: {exc}); marking empty")
                result = {x: [] for x in batch}
            for x in batch:
                plan[x] = result.get(x, [])
            _save_ckpt(plan)
            rate = (idx + 1) * args.batch / max(1e-6, time.time() - t0)
            print(f"  batch {idx + 1}: {(idx + 1) * args.batch}/{len(todo)} @ {rate:.0f}/s")

    n_split = sum(1 for v in plan.values() if v)
    n_drop = sum(1 for v in plan.values() if not v)
    n_atomic = sum(len(v) for v in plan.values())
    print(f"\nplan: split={n_split}  drop={n_drop}  total_atomic_ingredients={n_atomic}")
    print("sample splits:")
    for k, v in list(plan.items())[:10]:
        print(f"  {k[:60]!r:65s} -> {v}")

    if not args.write:
        print("[dry-run] re-run with --write to apply.")
        return 0

    # Apply
    rows_split = [{"raw_name": k, "atomic": v} for k, v in plan.items() if v]
    names_drop = [k for k, v in plan.items() if not v]
    try:
        with drv.session() as s:
            if rows_split:
                for i in range(0, len(rows_split), 50):
                    s.run(APPLY_CYPHER, rows=rows_split[i : i + 50]).consume()
                print(f"applied {len(rows_split)} split entries")
            if names_drop:
                s.run(DROP_OLD_CYPHER, names=names_drop).consume()
                print(f"dropped {len(names_drop)} modifier-only nodes' rels")
            # also drop the old compound rels for split entries (the MERGE created new rels but
            # the old ones are still there; we need to remove rels pointing to the OLD nodes)
            old_names = [r["raw_name"] for r in rows_split]
            if old_names:
                s.run(DROP_OLD_CYPHER, names=old_names).consume()
                print(f"dropped old compound rels for {len(old_names)} split entries")
            # cleanup orphans
            total = 0
            while True:
                rec = s.run(DROP_ORPHANS).single()
                d = rec["deleted"] if rec else 0
                total += d
                if not d:
                    break
            print(f"orphans deleted: {total}")
    finally:
        drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
