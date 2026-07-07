#!/usr/bin/env python3
"""Generate HealthyFoods gluten-free-option tags from recipe text.

The source-provided dietary badge is deliberately not used as an input. A
recipe qualifies only when its description or notes explicitly describe a
gluten-free adaptation, substitution, choice, or ingredient check.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

if __package__ in (None, ""):
    # Avoid scripts/neo4j shadowing the installed neo4j driver on direct runs.
    sys.path.remove(str(Path(__file__).resolve().parent))

from neo4j import GraphDatabase

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data/HealthyFoods/HealthyFood_recipes_clean.json"
DEFAULT_OUTPUT = (
    REPO_ROOT / "section5_outputs/repro_gluten_free_option_predictions.csv"
)
TAG_NAME = "gluten_free_option"

GLUTEN_FREE = r"gluten[ -]?free"
OPTION_PATTERNS = (
    re.compile(
        rf"\b(?:make|made|making|adapt|adapted|convert|converted)\b"
        rf".{{0,100}}\b{GLUTEN_FREE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:can|could|may|to)\s+be\b.{{0,80}}\b{GLUTEN_FREE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:use|using|choose|check|replace|substitute|swap|serve)\b"
        rf".{{0,120}}\b{GLUTEN_FREE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{GLUTEN_FREE}\b.{{0,120}}"
        r"\b(?:instead|alternative|option|version|variety|substitute|"
        r"replacement)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:for|as)\s+(?:a\s+)?{GLUTEN_FREE}\b",
        re.IGNORECASE,
    ),
)


def _text_fields(recipe: dict) -> list[str]:
    notes = recipe.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    return [
        str(recipe.get("description") or ""),
        *(str(note) for note in notes),
    ]


def find_gluten_free_option_evidence(recipe: dict) -> str | None:
    """Return matched adaptation evidence, or None if no option is described."""
    for text in _text_fields(recipe):
        normalized = " ".join(text.split())
        for pattern in OPTION_PATTERNS:
            match = pattern.search(normalized)
            if match:
                start = max(0, match.start() - 80)
                end = min(len(normalized), match.end() + 80)
                return normalized[start:end]
    return None


def generate_predictions(recipes: Iterable[dict]) -> list[dict[str, str]]:
    predictions = []
    for recipe in recipes:
        evidence = find_gluten_free_option_evidence(recipe)
        if evidence:
            predictions.append(
                {
                    "recipe_id": str(recipe["recipe_id"]),
                    "title": str(recipe.get("title") or ""),
                    "generated_tag": TAG_NAME,
                    "evidence": evidence,
                }
            )
    return predictions


def _load_recipes(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.values()) if isinstance(payload, dict) else payload


def _write_csv(path: Path, predictions: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["recipe_id", "title", "generated_tag", "evidence"],
        )
        writer.writeheader()
        writer.writerows(predictions)


def _write_neo4j(
    predictions: list[dict[str, str]],
    uri: str,
    username: str,
    password: str | None,
    no_auth: bool,
) -> int:
    auth = None if no_auth else (username, password)
    if not no_auth and not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or use --no-auth."
        )
    driver = GraphDatabase.driver(uri, auth=auth)
    delete_query = """
    MATCH (:Recipe)-[rel:HAS_TAG]->(:Tag {name: $tag_name})
    DELETE rel
    """
    write_query = """
    UNWIND $rows AS row
    MATCH (r:Recipe {source: "HealthyFoods"})
    WHERE toString(r.recipe_id) = row.recipe_id
    MERGE (t:Tag {name: $tag_name})
    SET t.category = "dietary_option"
    MERGE (r)-[rel:HAS_TAG]->(t)
    SET rel.generated_by = "explicit_recipe_text_rule",
        rel.evidence = row.evidence
    RETURN count(DISTINCT r) AS tagged
    """
    try:
        with driver.session() as session:
            session.run(delete_query, tag_name=TAG_NAME).consume()
            result = session.run(
                write_query,
                rows=predictions,
                tag_name=TAG_NAME,
            )
            row = result.single()
            return int(row["tagged"]) if row else 0
    finally:
        driver.close()


def main() -> None:
    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687")
    )
    parser.add_argument(
        "--neo4j-username",
        default=os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j")),
    )
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument(
        "--no-auth",
        action="store_true",
        default=os.getenv("NEO4J_NO_AUTH") == "1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create the reproducibility CSV without changing Neo4j.",
    )
    args = parser.parse_args()

    predictions = generate_predictions(_load_recipes(args.input))
    _write_csv(args.output, predictions)
    print(f"Wrote {len(predictions)} predictions to {args.output}")

    if not args.dry_run:
        tagged = _write_neo4j(
            predictions,
            args.neo4j_uri,
            args.neo4j_username,
            args.neo4j_password,
            args.no_auth,
        )
        print(f"Tagged {tagged} HealthyFoods recipes as {TAG_NAME}")


if __name__ == "__main__":
    main()
