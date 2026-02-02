import json
import os
from pathlib import Path
from typing import Iterable, Optional

from neo4j import GraphDatabase

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


# Purpose: Remove recipes with missing ingredients from JSON and Neo4j.

SOURCE_JSON = Path("data/processed/recipe1m/recipe1m-ex-limited-hummus-metadata.json")
REMOVED_IDS_JSON = Path("data/processed/recipe1m/recipes_missing_ingredients.json")


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool):
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or use NEO4J_NO_AUTH=1."
        )
    return GraphDatabase.driver(uri, auth=(username, password))


def _stream_json_array(path: Path) -> Iterable[dict]:
    decoder = json.JSONDecoder()
    buffer = ""
    in_array = False
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            buffer += chunk
            while True:
                if not in_array:
                    stripped = buffer.lstrip()
                    if not stripped:
                        break
                    if stripped[0] == "[":
                        buffer = stripped[1:]
                        in_array = True
                    else:
                        raise ValueError("Expected JSON array.")

                buffer = buffer.lstrip()
                if not buffer:
                    break
                if buffer[0] == "]":
                    return
                if buffer[0] == ",":
                    buffer = buffer[1:]
                    continue

                try:
                    obj, idx = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                buffer = buffer[idx:]
                yield obj

        buffer = buffer.lstrip()
        if buffer and buffer[0] != "]":
            raise ValueError("Incomplete JSON array.")


def _write_filtered_json(source: Path) -> list[str]:
    tmp_path = source.with_suffix(source.suffix + ".tmp")
    removed_ids: list[str] = []
    kept = 0
    total = 0

    with tmp_path.open("w", encoding="utf-8") as out:
        out.write("[")
        first = True
        for row in _stream_json_array(source):
            total += 1
            ingredients = row.get("ingredients") or row.get("ingredient_names") or []
            if not ingredients:
                recipe_id = row.get("id") or row.get("recipe_id")
                if recipe_id:
                    removed_ids.append(str(recipe_id))
                continue
            if not first:
                out.write(",")
            out.write(json.dumps(row, ensure_ascii=True))
            first = False
            kept += 1
        out.write("]")

    source.replace(tmp_path)
    REMOVED_IDS_JSON.parent.mkdir(parents=True, exist_ok=True)
    REMOVED_IDS_JSON.write_text(
        json.dumps(removed_ids, indent=2, ensure_ascii=True), encoding="utf-8"
    )
    print(f"Total: {total}")
    print(f"Kept: {kept}")
    print(f"Removed: {len(removed_ids)}")
    print(f"Removed ids written to: {REMOVED_IDS_JSON}")
    return removed_ids


def _delete_from_neo4j(driver, recipe_ids: list[str]) -> int:
    if not recipe_ids:
        return 0
    deleted = 0
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    DETACH DELETE r
    RETURN count(r) AS removed
    """
    with driver.session() as session:
        for i in range(0, len(recipe_ids), 500):
            batch = recipe_ids[i : i + 500]
            result = session.run(query, ids=batch)
            deleted += int(result.single()["removed"])
    return deleted


def main() -> None:
    if load_dotenv:
        load_dotenv()

    if not SOURCE_JSON.exists():
        raise FileNotFoundError(f"Missing source JSON: {SOURCE_JSON}")

    removed_ids = _write_filtered_json(SOURCE_JSON)

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"

    driver = _connect(uri, username, password, no_auth)
    try:
        deleted = _delete_from_neo4j(driver, removed_ids)
        print(f"Deleted from Neo4j: {deleted}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
