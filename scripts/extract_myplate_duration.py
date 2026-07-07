#!/usr/bin/env python3
"""Extract total cooking time in minutes from MyPlate recipe instructions via vLLM.

Reads instructions from Neo4j, calls mistral-24b on :8005, writes results to
data/MyPlate/myplate_duration.json, then bulk-sets duration_minutes in Neo4j.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from langchain_openai import ChatOpenAI
from neo4j import GraphDatabase

NEO4J_URI      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "password123")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8005/v1")
LLM_MODEL    = os.environ.get("LLM_MODEL", "mistral-24b")

OUT_FILE = Path("data/MyPlate/myplate_duration.json")
BATCH    = 500
DELAY    = 0.2

SYSTEM = "You extract total recipe time. Reply with a single integer (minutes only). No explanation."
PROMPT = "Recipe instructions:\n{instructions}\n\nTotal time in minutes (integer only):"


def get_recipes(driver) -> list[dict]:
    with driver.session() as s:
        result = s.run("""
MATCH (r:Recipe {source: 'MyPlate'})
WHERE r.duration_minutes IS NULL
RETURN r.recipe_id AS id, r.instructions AS instructions
""")
        return [{"recipe_id": row["id"], "instructions": row["instructions"]} for row in result
                if row["instructions"]]


def extract_minutes(llm: ChatOpenAI, instructions: str) -> int | None:
    try:
        msg = llm.invoke([
            ("system", SYSTEM),
            ("human", PROMPT.format(instructions=instructions[:3000])),
        ])
        raw = msg.content.strip().split()[0]
        return int(raw)
    except Exception:
        return None


def apply_to_neo4j(driver, durations: dict[str, int]):
    items = [{"rid": rid, "mins": mins} for rid, mins in durations.items()]
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        with driver.session() as s:
            s.run("""
UNWIND $rows AS row
MATCH (r:Recipe {recipe_id: row.rid})
SET r.duration_minutes = row.mins
""", rows=batch)
        print(f"  Neo4j updated: {min(i + BATCH, len(items))}/{len(items)}", flush=True)


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, int | None] = {}
    if OUT_FILE.exists():
        existing = json.loads(OUT_FILE.read_text())
        print(f"Resuming: {len(existing)} already done")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    recipes = get_recipes(driver)
    todo = [r for r in recipes if r["recipe_id"] not in existing]
    print(f"{len(todo)} recipes to process")

    llm = ChatOpenAI(
        base_url=LLM_BASE_URL,
        api_key="none",
        model=LLM_MODEL,
        temperature=0,
        max_tokens=8,
    )

    for i, rec in enumerate(todo):
        mins = extract_minutes(llm, rec["instructions"] or "")
        existing[rec["recipe_id"]] = mins
        status = f"{mins} min" if mins is not None else "failed"
        print(f"  [{i+1}/{len(todo)}] {rec['recipe_id']} → {status}", flush=True)

        if (i + 1) % 50 == 0:
            OUT_FILE.write_text(json.dumps(existing, indent=2))

        time.sleep(DELAY)

    OUT_FILE.write_text(json.dumps(existing, indent=2))

    valid = {rid: mins for rid, mins in existing.items() if mins is not None}
    print(f"\n{len(valid)}/{len(existing)} with valid duration — writing to Neo4j...")
    apply_to_neo4j(driver, valid)
    driver.close()

    # Re-apply 30_minutes_or_less tag
    driver2 = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver2.session() as s:
        r = s.run("""
MATCH (r:Recipe {source: 'MyPlate'})
WHERE r.duration_minutes IS NOT NULL AND r.duration_minutes < 30
MERGE (t:Tag {name: '30_minutes_or_less'})
MERGE (r)-[:HAS_TAG]->(t)
RETURN count(r) AS n
""")
        n = r.single()["n"]
    driver2.close()
    print(f"Tagged {n} MyPlate recipes as 30_minutes_or_less")
    print(f"Done → {OUT_FILE}")


if __name__ == "__main__":
    main()
