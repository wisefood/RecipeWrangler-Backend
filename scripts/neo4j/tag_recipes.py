import argparse
import os
import re
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


def _keyword_regex(keyword: str) -> str:
    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"\s+")
    return rf".*\b{escaped}(e?s)?\b.*"


PLANT_OR_CANONICAL_EXCLUSION_REGEXES = [
    r".*\bplant[ -]*based\b.*",
    r".*\bvegan\b.*",
    r".*\bvegetarian\b.*",
    r".*\b(coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"([ -]+(flavoured|flavored))?[ -]+(milk|cream|yogurt|yoghurt)\b.*",
    r".*\b(milk|cream|yogurt|yoghurt)[ -]+alternative\b.*",
    r".*\b(non[ -]*dairy|dairy[ -]*free)\b.*",
    r".*\b(peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter\b.*",
    r".*\bbutter[ -]*beans?\b.*",
    r".*\bbeans?,[ -]*butter\b.*",
    r".*\bbutternut\b.*",
    r".*\bcream[ -]+substitute\b.*",
    r".*\bcream[ -]+rice\b.*",
    r".*\bbutter[ -]+almond\b.*",
    r".*\bquorn\b.*",
    r".*\bcream[ -]+parsley\b.*",
    r"^(chicken chilli|chicken cube|beef spread|pork sauce)$",
    r"^(chicken stock vegetable|stock vegetable chicken|vegetable chicken stock"
    r"|vegetable stock beef)$",
]


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


def _tag_gluten_free(driver) -> int:
    query = """
    MATCH (r:Recipe)
    WHERE EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(a:Allergen)
        WHERE a.name IN ["wheat", "gluten"]
    }
    MERGE (t:Tag {name: "gluten_free"})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(query)
        return int(result.single()["tagged"])


def _tag_vegetarian_or_vegan(driver) -> int:
    query = """
    MATCH (r:Recipe)-[:HAS_TAG]->(source_tag:Tag)
    WHERE source_tag.name IN ["vegetarian", "vegan"]
    WITH DISTINCT r
    MERGE (t:Tag {name: "vegetarian_or_vegan"})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(r) AS tagged
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
    exclude_name_regexes: list[str],
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
          AND none(pattern IN $exclude_name_regexes
                   WHERE toLower(i.name) =~ pattern)
          AND NOT EXISTS {
            MATCH (i)-[:HAS_CLASS]->(f2:FoodOnClass)
            MATCH (f2)-[:SUBCLASS_OF*0..]->(e:FoodOnClass)
            WHERE e.foodon_id IN $exclude_roots
          }
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
        WHERE i2.name IS NOT NULL
          AND any(pattern IN $forbidden_keyword_regexes
                  WHERE toLower(i2.name) =~ pattern)
          AND NOT any(x IN $exclude_keywords WHERE toLower(i2.name) CONTAINS x)
          AND none(pattern IN $exclude_name_regexes
                   WHERE toLower(i2.name) =~ pattern)
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
            forbidden_keyword_regexes=[
                _keyword_regex(k) for k in forbidden_keywords
            ],
            exclude_keywords=[k.casefold() for k in exclude_keywords],
            exclude_name_regexes=exclude_name_regexes,
        )
        return int(result.single()["tagged"])


def _clear_recipe_tags(driver, tag_names: list[str]) -> int:
    query = """
    MATCH (:Recipe)-[rel:HAS_TAG]->(t:Tag)
    WHERE t.name IN $tag_names
    DELETE rel
    RETURN count(rel) AS deleted
    """
    with driver.session() as session:
        result = session.run(query, tag_names=tag_names)
        return int(result.single()["deleted"])


def _tag_pescatarian(
    driver,
    forbidden_roots: list[str],
    exclude_roots: list[str],
    forbidden_foodon_keywords: list[str],
    allow_foodon_keywords: list[str],
    forbidden_ingredient_keywords: list[str],
    exclude_ingredient_keywords: list[str],
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
        WHERE (
            a.foodon_id IN $forbidden_roots
            OR (
                a.name IS NOT NULL
                AND any(k IN $forbidden_foodon_keywords WHERE toLower(a.name) CONTAINS k)
                AND NOT any(ok IN $allow_foodon_keywords WHERE toLower(a.name) CONTAINS ok)
            )
        )
        AND NOT EXISTS {
            MATCH (i)-[:HAS_CLASS]->(f2:FoodOnClass)
            MATCH (f2)-[:SUBCLASS_OF*0..]->(e:FoodOnClass)
            WHERE e.foodon_id IN $exclude_roots
        }
    }
    AND NOT EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i2:Ingredient)
        WHERE i2.name IS NOT NULL
          AND any(k IN $forbidden_ingredient_keywords WHERE toLower(i2.name) CONTAINS k)
          AND NOT any(x IN $exclude_ingredient_keywords WHERE toLower(i2.name) CONTAINS x)
    }
    MERGE (t:Tag {name: "pescatarian"})
    SET t.category = "dietary"
    MERGE (r)-[:HAS_TAG]->(t)
    RETURN count(distinct r) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            forbidden_roots=forbidden_roots,
            exclude_roots=exclude_roots,
            forbidden_foodon_keywords=[k.casefold() for k in forbidden_foodon_keywords],
            allow_foodon_keywords=[k.casefold() for k in allow_foodon_keywords],
            forbidden_ingredient_keywords=[
                k.casefold() for k in forbidden_ingredient_keywords
            ],
            exclude_ingredient_keywords=[
                k.casefold() for k in exclude_ingredient_keywords
            ],
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
    parser = argparse.ArgumentParser(
        description="Generate recipe-level dietary and convenience tags."
    )
    parser.add_argument(
        "--replace-dietary",
        action="store_true",
        help="Delete existing dietary tag edges before rebuilding them.",
    )
    parser.add_argument(
        "--tags",
        nargs="+",
        choices=[
            "dairy_free",
            "nut_free",
            "gluten_free",
            "vegetarian",
            "vegan",
            "vegetarian_or_vegan",
            "pescatarian",
            "30_minutes_or_less",
            "5_ingredients_or_less",
        ],
        help="Only rebuild the selected recipe tags (default: all).",
    )
    args = parser.parse_args()

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
        "gelatin",
        "whey",
        "casein",
    ]

    plant_based_exclude_keywords = [
        "plant-based",
        "plant based",
        "vegan",
    ]
    pescatarian_forbidden_roots = [
        "FOODON_00002671",  # animal meat food product (present in ontology, sparse in this graph)
    ]
    pescatarian_exclude_roots = [
        "FOODON_00001046",  # animal seafood product
        "FOODON_00001248",  # fish food product
        "FOODON_00001293",  # shellfish food product
        "FOODON_00002129",  # plant based meat product analog
        "FOODON_00002134",  # plant based seafood product analog
        "FOODON_00002260",  # soybean based meat product analog
    ]
    pescatarian_forbidden_foodon_keywords = [
        "animal meat",
        "beef",
        "pork",
        "ham",
        "bacon",
        "sausage",
        "chicken",
        "turkey",
        "duck",
        "goose",
        "lamb",
        "mutton",
        "veal",
        "venison",
        "goat",
    ]
    pescatarian_allow_foodon_keywords = [
        "fish",
        "seafood",
        "shellfish",
        "crustacean",
        "mollusk",
        "shrimp",
        "prawn",
        "crab",
        "lobster",
        "clam",
        "mussel",
        "oyster",
        "scallop",
        "squid",
        "octopus",
    ]
    pescatarian_forbidden_ingredient_keywords = [
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
        "venison",
        "goat",
        "meat",
        "sausage",
        "pepperoni",
        "prosciutto",
    ]
    pescatarian_exclude_ingredient_keywords = plant_based_exclude_keywords + [
        "imitation meat",
    ]

    driver = _connect(uri, username, password, no_auth)
    try:
        _ensure_constraints(driver)
        if args.replace_dietary:
            selected_dietary = set(
                args.tags
                or [
                    "dairy_free",
                    "nut_free",
                    "gluten_free",
                    "vegetarian",
                    "vegan",
                    "vegetarian_or_vegan",
                    "pescatarian",
                ]
            )
            deleted = _clear_recipe_tags(
                driver,
                sorted(
                    selected_dietary
                    & {
                        "dairy_free",
                        "nut_free",
                        "gluten_free",
                        "vegetarian",
                        "vegan",
                        "vegetarian_or_vegan",
                        "pescatarian",
                    }
                ),
            )
            print(f"deleted existing dietary tag edges: {deleted}")
        tasks = [
            ("dairy_free", lambda: _tag_dairy_free(driver)),
            ("nut_free", lambda: _tag_nut_free(driver)),
            ("gluten_free", lambda: _tag_gluten_free(driver)),
            (
                "vegetarian",
                lambda: _tag_foodon_free(
                    driver,
                    "vegetarian",
                    vegetarian_forbidden_roots,
                    plant_based_analog_roots,
                    vegetarian_forbidden_keywords,
                    plant_based_exclude_keywords,
                    PLANT_OR_CANONICAL_EXCLUSION_REGEXES,
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
                    PLANT_OR_CANONICAL_EXCLUSION_REGEXES,
                ),
            ),
            (
                "vegetarian_or_vegan",
                lambda: _tag_vegetarian_or_vegan(driver),
            ),
            (
                "pescatarian",
                lambda: _tag_pescatarian(
                    driver,
                    pescatarian_forbidden_roots,
                    pescatarian_exclude_roots,
                    pescatarian_forbidden_foodon_keywords,
                    pescatarian_allow_foodon_keywords,
                    pescatarian_forbidden_ingredient_keywords,
                    pescatarian_exclude_ingredient_keywords,
                ),
            ),
            ("30_minutes_or_less", lambda: _tag_30_minutes_or_less(driver)),
            ("5_ingredients_or_less", lambda: _tag_five_ingredients_or_less(driver)),
        ]
        if args.tags:
            selected_tags = set(args.tags)
            tasks = [task for task in tasks if task[0] in selected_tags]
        iterator = tqdm(tasks, desc="Tagging recipes") if tqdm else tasks
        for name, fn in iterator:
            tagged = fn()
            print(f"{name}: {tagged}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
