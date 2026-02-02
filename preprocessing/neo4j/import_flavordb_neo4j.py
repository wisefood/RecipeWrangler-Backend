import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from neo4j import GraphDatabase


# Purpose: Import FlavorDB nodes/edges (and optional ingredient links) into Neo4j.

def _chunks(rows: Iterable[dict], size: int) -> Iterable[List[dict]]:
    batch: List[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load_ingredient_categories(path: Path) -> Dict[str, str]:
    categories: Dict[str, str] = {}
    if not path.exists():
        return categories
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ingredient = (row.get("ingredient") or "").strip()
            category = (row.get("category") or "").strip()
            if ingredient and category:
                categories[ingredient] = category
    return categories


def _load_hub_classifications(path: Path) -> Dict[str, List[str]]:
    classifications: Dict[str, List[str]] = defaultdict(list)
    if not path.exists():
        return classifications
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        categories = reader.fieldnames or []
        for row in reader:
            for category in categories:
                ingredient = (row.get(category) or "").strip()
                if ingredient:
                    classifications[ingredient].append(category)
    return classifications


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool) -> GraphDatabase.driver:
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or pass --neo4j-password, "
            "or use --no-auth if your instance allows it."
        )
    return GraphDatabase.driver(uri, auth=(username, password))


def _ensure_constraints(driver: GraphDatabase.driver) -> None:
    statements = [
        (
            "CREATE CONSTRAINT flavor_ingredient_id IF NOT EXISTS "
            "FOR (n:FlavorIngredient) REQUIRE n.flavordb_id IS UNIQUE"
        ),
        (
            "CREATE CONSTRAINT flavor_compound_id IF NOT EXISTS "
            "FOR (n:FlavorCompound) REQUIRE n.flavordb_id IS UNIQUE"
        ),
    ]
    with driver.session() as session:
        for statement in statements:
            session.run(statement)


def _import_nodes(
    driver: GraphDatabase.driver,
    nodes_path: Path,
    categories: Dict[str, str],
    hub_classes: Dict[str, List[str]],
    batch_size: int,
) -> None:
    ingredients: List[dict] = []
    compounds: List[dict] = []
    with nodes_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            node_type = (row.get("node_type") or "").strip()
            flavordb_id = int(row["flavordb_id"])
            name = (row.get("flavordb_name") or "").strip()
            if node_type == "ingredient":
                ingredients.append(
                    {
                        "id": flavordb_id,
                        "name": name,
                        "is_hub": (row.get("is_hub") or "").strip(),
                        "category": categories.get(name),
                        "hub_categories": hub_classes.get(name, []),
                    }
                )
            elif node_type == "compound":
                compounds.append({"id": flavordb_id, "name": name})

    with driver.session() as session:
        if ingredients:
            query = """
            UNWIND $rows AS row
            MERGE (n:FlavorIngredient {flavordb_id: row.id})
            SET n.name = row.name,
                n.is_hub = row.is_hub,
                n.category = row.category,
                n.hub_categories = row.hub_categories
            """
            for batch in _chunks(ingredients, batch_size):
                session.run(query, rows=batch)

        if compounds:
            query = """
            UNWIND $rows AS row
            MERGE (n:FlavorCompound {flavordb_id: row.id})
            SET n.name = row.name
            """
            for batch in _chunks(compounds, batch_size):
                session.run(query, rows=batch)


def _import_edges(
    driver: GraphDatabase.driver,
    edges_path: Path,
    batch_size: int,
) -> None:
    edge_batches = {"ingr-ingr": [], "ingr-fcomp": [], "ingr-dcomp": []}
    with edges_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            edge_type = (row.get("edge_type") or "").strip()
            score_raw = (row.get("score") or "").strip()
            score_value = float(score_raw) if score_raw else None
            payload = {
                "id_1": int(row["id_1"]),
                "id_2": int(row["id_2"]),
                "score": score_value,
                "edge_type": edge_type,
            }
            if edge_type in edge_batches:
                edge_batches[edge_type].append(payload)

    with driver.session() as session:
        if edge_batches["ingr-ingr"]:
            query = """
            UNWIND $rows AS row
            MATCH (a:FlavorIngredient {flavordb_id: row.id_1})
            MATCH (b:FlavorIngredient {flavordb_id: row.id_2})
            MERGE (a)-[r:FLAVOR_PAIR]->(b)
            SET r.score = row.score,
                r.source = "FlavorDB"
            """
            for batch in _chunks(edge_batches["ingr-ingr"], batch_size):
                session.run(query, rows=batch)

        for edge_type in ("ingr-fcomp", "ingr-dcomp"):
            if not edge_batches[edge_type]:
                continue
            compound_kind = "food" if edge_type == "ingr-fcomp" else "drug"
            query = """
            UNWIND $rows AS row
            MATCH (a:FlavorIngredient {flavordb_id: row.id_1})
            MATCH (b:FlavorCompound {flavordb_id: row.id_2})
            MERGE (a)-[r:HAS_COMPOUND]->(b)
            SET r.score = row.score,
                r.edge_type = row.edge_type,
                r.compound_kind = $compound_kind,
                r.source = "FlavorDB"
            """
            for batch in _chunks(edge_batches[edge_type], batch_size):
                session.run(query, rows=batch, compound_kind=compound_kind)


def _import_ingredient_links(
    driver: GraphDatabase.driver,
    processed_map_path: Path,
    flavor_map_path: Path,
    batch_size: int,
) -> None:
    with processed_map_path.open(encoding="utf-8") as handle:
        processed_map = json.load(handle)
    with flavor_map_path.open(encoding="utf-8") as handle:
        flavor_map = json.load(handle)

    rows: List[dict] = []
    missing_processed = 0
    for mskg_id, flavor in flavor_map.items():
        processed = processed_map.get(mskg_id)
        if not processed:
            missing_processed += 1
            continue
        name = processed.get("name")
        if not name:
            continue
        flavordb_id = flavor.get("flavordb_id")
        if flavordb_id is None:
            continue
        rows.append(
            {
                "name_lower": name.casefold(),
                "flavordb_id": int(flavordb_id),
                "cosine_similarity": flavor.get("cosine_similarity"),
            }
        )

    if not rows:
        print("No Ingredient -> FlavorIngredient links to import.")
        return

    query = """
    UNWIND $rows AS row
    MATCH (i:Ingredient)
    WHERE toLower(i.name) = row.name_lower
    MATCH (f:FlavorIngredient {flavordb_id: row.flavordb_id})
    MERGE (i)-[r:MAPS_TO_FLAVORDB]->(f)
    SET r.cosine_similarity = row.cosine_similarity,
        r.source = "FlavorDB"
    """
    with driver.session() as session:
        for batch in _chunks(rows, batch_size):
            session.run(query, rows=batch)
    if missing_processed:
        print(f"Skipped {missing_processed} flavor links with no processed map entry.")

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import FlavorDB nodes and edges into Neo4j."
    )
    parser.add_argument(
        "--nodes",
        type=Path,
        default=Path("data/FlavorDB/nodes_191120.csv"),
        help="Path to FlavorDB nodes CSV.",
    )
    parser.add_argument(
        "--edges",
        type=Path,
        default=Path("data/FlavorDB/edges_191120.csv"),
        help="Path to FlavorDB edges CSV.",
    )
    parser.add_argument(
        "--ingredient-categories",
        type=Path,
        default=Path("data/FlavorDB/dict_ingr2cate - Top300+FDB400+HyperFoods104=616.csv"),
        help="Path to ingredient category mapping CSV.",
    )
    parser.add_argument(
        "--hub-classifications",
        type=Path,
        default=Path("data/FlavorDB/node_classification_hub.csv"),
        help="Path to hub classification CSV.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for Neo4j UNWIND inserts.",
    )
    parser.add_argument(
        "--neo4j-uri",
        default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j bolt URI (or set NEO4J_URI).",
    )
    parser.add_argument(
        "--neo4j-user",
        default=os.getenv("NEO4J_USER", "neo4j"),
        help="Neo4j username (or set NEO4J_USER).",
    )
    parser.add_argument(
        "--neo4j-password",
        default=os.getenv("NEO4J_PASSWORD", ""),
        help="Neo4j password (or set NEO4J_PASSWORD).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Connect without authentication (only if Neo4j allows it).",
    )
    parser.add_argument(
        "--skip-constraints",
        action="store_true",
        help="Skip creating uniqueness constraints.",
    )
    parser.add_argument(
        "--processed-map",
        type=Path,
        default=Path("data/MISKG/processed_id_recipe1m_map.json"),
        help="Path to processed id -> recipe1m map JSON.",
    )
    parser.add_argument(
        "--flavor-map",
        type=Path,
        default=Path("data/MISKG/ingredient_to_flavordb.json"),
        help="Path to ingredient -> FlavorDB mapping JSON.",
    )
    parser.add_argument(
        "--skip-ingredient-links",
        action="store_true",
        help="Skip linking Ingredient nodes to FlavorIngredient nodes.",
    )
    args = parser.parse_args()

    driver = _connect(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.no_auth)
    try:
        if not args.skip_constraints:
            _ensure_constraints(driver)
        categories = _load_ingredient_categories(args.ingredient_categories)
        hub_classes = _load_hub_classifications(args.hub_classifications)
        _import_nodes(driver, args.nodes, categories, hub_classes, args.batch_size)
        _import_edges(driver, args.edges, args.batch_size)
        if not args.skip_ingredient_links:
            _import_ingredient_links(
                driver,
                args.processed_map,
                args.flavor_map,
                args.batch_size,
            )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
