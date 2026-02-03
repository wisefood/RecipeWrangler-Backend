import os
from typing import Optional

from neo4j import GraphDatabase
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None


# Purpose: Tag recipes based on ingredient allergen tags.


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool):
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or use NEO4J_NO_AUTH=1."
        )
    return GraphDatabase.driver(uri, auth=(username, password))


def _ensure_constraints(driver) -> None:
    statement = (
        "CREATE CONSTRAINT tag_name IF NOT EXISTS "
        "FOR (n:Tag) REQUIRE n.name IS UNIQUE"
    )
    with driver.session() as session:
        session.run(statement)


def _tag_dairy_free(driver) -> int:
    query = """
    MATCH (r:Recipe)
    WHERE EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(:Allergen {name: "milk"})
    }
    MERGE (t:Tag {name: "dairy_free"})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(query)
        return int(result.single()["tagged"])


def _tag_nut_free(driver) -> int:
    query = """
    MATCH (r:Recipe)
    WHERE EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
        WHERE a.name IN ["peanut", "tree_nut"]
    }
    MERGE (t:Tag {name: "nut_free"})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(query)
        return int(result.single()["tagged"])


def _tag_foodon_free(
    driver,
    tag_name: str,
    forbidden_roots: list[str],
    exclude_roots: list[str],
    forbidden_keywords: list[str],
    exclude_keywords: list[str],
) -> int:
    query = """
    MATCH (r:Recipe)
    WHERE EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        MATCH (i)-[:HAS_CLASS]->(f:FoodOnClass)
        MATCH (f)-[:SUBCLASS_OF*0..]->(a:FoodOnClass)
        WHERE a.foodon_id IN $forbidden_roots
          AND NOT EXISTS {
            MATCH (i)-[:HAS_CLASS]->(f2:FoodOnClass)
            MATCH (f2)-[:SUBCLASS_OF*0..]->(e:FoodOnClass)
            WHERE e.foodon_id IN $exclude_roots
          }
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
        WHERE i2.name IS NOT NULL
          AND any(k IN $forbidden_keywords WHERE toLower(i2.name) CONTAINS k)
          AND NOT any(x IN $exclude_keywords WHERE toLower(i2.name) CONTAINS x)
    }
    MERGE (t:Tag {name: $tag_name})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            tag_name=tag_name,
            forbidden_roots=forbidden_roots,
            exclude_roots=exclude_roots,
            forbidden_keywords=[k.casefold() for k in forbidden_keywords],
            exclude_keywords=[k.casefold() for k in exclude_keywords],
        )
        return int(result.single()["tagged"])


def _tag_30_minutes_or_less(driver) -> int:
    query = """
    MATCH (r:Recipe)
    WITH r, coalesce(r.duration, r.total_time, r.duration_min) AS minutes
    WHERE minutes IS NOT NULL AND minutes <= 30
    MERGE (t:Tag {name: "30_minutes_or_less"})
    SET t.category = "time"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(query)
        return int(result.single()["tagged"])


def _tag_five_ingredients_or_less(driver) -> int:
    query = """
    MATCH (r:Recipe)
    OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
    WITH r, count(distinct i) AS ingredient_count
    WHERE ingredient_count > 0 AND ingredient_count <= 5
    MERGE (t:Tag {name: "5_ingredients_or_less"})
    SET t.category = "simplicity"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(query)
        return int(result.single()["tagged"])


def main() -> None:
    if load_dotenv:
        load_dotenv()
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"

    plant_based_analog_roots = [
        "FOODON_00002129",  # plant based meat product analog
        "FOODON_00002134",  # plant based seafood product analog
        "FOODON_00002260",  # soybean based meat product analog
    ]

    vegetarian_forbidden_roots = [
        "FOODON_00002671",  # animal meat food product
        "FOODON_00001046",  # animal seafood product
        "FOODON_00001248",  # fish food product
        "FOODON_00001293",  # shellfish food product
    ]

    vegetarian_forbidden_keywords = [
        "beef",
        "pork",
        "bacon",
        "ham",
        "turkey",
        "chicken",
        "duck",
        "goose",
        "lamb",
        "mutton",
        "veal",
        "meat",
        "sausage",
        "pepperoni",
        "prosciutto",
        "fish",
        "salmon",
        "tuna",
        "cod",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "shellfish",
    ]

    vegan_forbidden_roots = [
        "FOODON_00002671",  # animal meat food product
        "FOODON_00001046",  # animal seafood product
        "FOODON_00001248",  # fish food product
        "FOODON_00001293",  # shellfish food product
        "FOODON_00001256",  # dairy food product
        "FOODON_00001274",  # egg food product
        "FOODON_00001178",  # honey food product
        "FOODON_00001900",  # gelatin refined food product
        "FOODON_00001899",  # gelatin dessert food product
        "CHEBI_5291",       # gelatin
    ]

    vegan_forbidden_keywords = vegetarian_forbidden_keywords + [
        "milk",
        "cheese",
        "butter",
        "cream",
        "yogurt",
        "egg",
        "honey",
        "gelatin",
        "whey",
        "casein",
    ]

    plant_based_exclude_keywords = [
        "plant-based",
        "plant based",
        "vegan",
    ]

    driver = _connect(uri, username, password, no_auth)
    try:
        _ensure_constraints(driver)
        tasks = [
            ("dairy_free", lambda: _tag_dairy_free(driver)),
            ("nut_free", lambda: _tag_nut_free(driver)),
            (
                "vegetarian",
                lambda: _tag_foodon_free(
                    driver,
                    "vegetarian",
                    vegetarian_forbidden_roots,
                    plant_based_analog_roots,
                    vegetarian_forbidden_keywords,
                    plant_based_exclude_keywords,
                ),
            ),
            (
                "vegan",
                lambda: _tag_foodon_free(
                    driver,
                    "vegan",
                    vegan_forbidden_roots,
                    plant_based_analog_roots,
                    vegan_forbidden_keywords,
                    plant_based_exclude_keywords,
                ),
            ),
            ("30_minutes_or_less", lambda: _tag_30_minutes_or_less(driver)),
            ("5_ingredients_or_less", lambda: _tag_five_ingredients_or_less(driver)),
        ]
        iterator = tqdm(tasks, desc="Tagging recipes") if tqdm else tasks
        for name, fn in iterator:
            tagged = fn()
            print(f"{name}: {tagged}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
