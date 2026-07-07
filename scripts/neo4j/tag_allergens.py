import argparse
import os
import re
from typing import Optional
from pathlib import Path

from neo4j import GraphDatabase
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None
try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None


# Purpose: Tag ingredients with allergen evidence (FoodOn ancestry + keyword fallback).

ALLERGENS = {
    "milk": {
        "roots": [
            "FOODON_00001257",  # milk or milk based food product
            "FOODON_00001256",  # dairy food product
            "FOODON_00001771",  # cow milk based food product
            "FOODON_00001118",  # cattle dairy food product
        ],
        "keywords": [
            "milk",
            "cheese",
            "butter",
            "cream",
            "yogurt",
            "whey",
            "casein",
            "lactose",
            "ghee",
            "curd",
            "kefir",
        ],
    },
    "egg": {
        "roots": [
            "FOODON_00001274",  # egg food product
            "FOODON_00001105",  # avian egg food product
            "FOODON_02010002",  # animal egg
        ],
        "keywords": [
            "egg",
            "egg white",
            "egg yolk",
            "omelet",
            "mayonnaise",
            "aioli",
            "meringue",
            "albumen",
        ],
    },
    "peanut": {
        "roots": [
            "FOODON_00002099",  # peanut food product
            "FOODON_00003206",  # peanut
            "FOODON_00002098",  # peanut fat or oil refined food product
            "FOODON_00005586",  # peanut flour
        ],
        "keywords": [
            "peanut",
            "peanut butter",
            "groundnut",
            "arachis",
        ],
    },
    "tree_nut": {
        "roots": [
            "FOODON_00001587",  # almond food product
            "FOODON_00002338",  # walnut food product
            "FOODON_00002107",  # pecan nut food product
            "FOODON_00001688",  # cashew nut food product
            "FOODON_00003690",  # pistachio nut food product
        ],
        "keywords": [
            "almond",
            "walnut",
            "pecan",
            "cashew",
            "pistachio",
            "hazelnut",
            "macadamia",
            "brazil nut",
            "pine nut",
        ],
    },
    "wheat": {
        "roots": [
            "FOODON_00001141",  # wheat food product
            "FOODON_00001210",  # wheat flour food product
            "FOODON_00002347",  # wheat based bakery food product
            "FOODON_00002349",  # wheat based gravy or sauce food product
            "FOODON_00002351",  # wheat bread food product
            "FOODON_00002354",  # wheat pasta
            "FOODON_00001825",  # durum wheat food product
        ],
        "keywords": [
            "wheat",
            "whole wheat",
            "durum",
            "semolina",
            "farina",
            "graham",
            "spelt",
            "bulgur",
            "couscous",
            "seitan",
            "gluten",
            "flour",
            "bread",
            "breadcrumbs",
            "breading",
            "batter",
            "roux",
            "pasta",
            "noodle",
        ],
    },
    "soy": {
        "roots": [
            "FOODON_00002266",  # soybean food product
            "FOODON_00001078",  # fermented soybean food product
            "FOODON_00001235",  # soy sauce food product
            "FOODON_03302389",  # soybean beverage
            "FOODON_03302776",  # soybean oil
            "FOODON_03310553",  # soy protein isolate
            "FOODON_03310368",  # soy protein
            "FOODON_03306653",  # soy lecithin spread
            "FOODON_03305289",  # soybean milk
            "FOODON_03310002",  # soybean paste
        ],
        "keywords": [
            "soy",
            "soya",
            "soybean",
            "edamame",
            "tofu",
            "tempeh",
            "miso",
            "soy sauce",
            "tamari",
            "shoyu",
            "soy lecithin",
            "lecithin (soy)",
            "textured vegetable protein",
            "tvp",
            "soy protein",
            "soy isolate",
            "soy flour",
            "soy oil",
            "soy milk",
            "soy yogurt",
            "natto",
        ],
    },
    "fish": {
        "roots": [
            "FOODON_00001248",  # fish food product
            "FOODON_00001055",  # sea water fish food product
            "FOODON_00001249",  # freshwater fish food product
            "FOODON_03315173",  # fish product (unspecified species)
            "FOODON_00001661",  # bony fish food product
            "FOODON_00001054",  # fermented fish or seafood food product
            "FOODON_03317197",  # fish sauce
        ],
        "keywords": [
            "fish",
            "cod",
            "bass",
            "flounder",
            "salmon",
            "tuna",
            "haddock",
            "tilapia",
            "anchovy",
            "sardine",
            "trout",
            "mackerel",
            "halibut",
            "pollock",
            "catfish",
            "swordfish",
            "fish sauce",
        ],
    },
    "crustacean_shellfish": {
        "roots": [
            "FOODON_00001792",  # crustacean food product
            "FOODON_02021444",  # crab food product
            "FOODON_00002007",  # lobster food product
            "FOODON_00002239",  # shrimp food product
        ],
        "keywords": [
            "crab",
            "lobster",
            "shrimp",
            "prawn",
            "crustacean",
            "langostino",
        ],
    },
    "sesame": {
        "roots": [
            "FOODON_00002232",  # sesame food product
            "FOODON_03310306",  # sesame seed
            "FOODON_03304152",  # sesame oil
            "FOODON_00004525",  # sesame butter
            "FOODON_00005500",  # sesame flour
            "FOODON_03304154",  # sesame seed paste
        ],
        "keywords": [
            "sesame",
            "tahini",
            "sesame oil",
            "sesame seed",
            "sesame paste",
        ],
    },
    "gluten": {
        "roots": [
            "FOODON_03420177",  # gluten
            "FOODON_00001907",  # gluten refined food product
            "FOODON_03310809",  # wheat gluten
            "FOODON_03310808",  # soy gluten
            "FOODON_03302452",  # gluten bread
            "FOODON_03302453",  # gluten flour
            "FOODON_03306200",  # gluten noodle
            "FOODON_00001275",  # wheat (big three)
            "FOODON_00001217",  # barley (big three)
            "FOODON_00001272",  # rye (big three)
            "FOODON_00001254",  # oats (cross-contamination risk)
        ],
        "keywords": [
            "gluten",
            "wheat",
            "barley",
            "rye",
            "spelt",
            "kamut",
            "farro",
            "durum",
            "bulgur",
            "malt",
            "soy sauce",
            "seitan",
            "brewer's yeast",
            "modified food starch",
            "roux",
            "gravy",
            "oats",
        ],
    },
    "celery": {
        "roots": [
            "FOODON_00001704",  # celery food product
            "FOODON_00001705",  # leaf celery food product
        ],
        "keywords": [
            "celery",
            "celeriac",
            "celery seed",
            "celery salt",
        ],
    },
    "mustard": {
        "roots": [
            "FOODON_00002053",  # mustard food product
        ],
        "keywords": [
            "mustard",
            "mustard seed",
            "mustard powder",
            "mustard flour",
        ],
    },
    "sulphites": {
        # Sulphites are additives rather than a FoodOn food-product branch, so
        # they are detected from explicit ingredient/additive names.
        "roots": [],
        "keywords": [
            "sulphite",
            "sulphites",
            "sulfite",
            "sulfites",
            "sulphur dioxide",
            "sulfur dioxide",
            "metabisulphite",
            "metabisulfite",
            "bisulphite",
            "bisulfite",
            "sodium sulphite",
            "sodium sulfite",
            "potassium sulphite",
            "potassium sulfite",
            "e220",
            "e221",
            "e222",
            "e223",
            "e224",
            "e225",
            "e226",
            "e227",
            "e228",
        ],
    },
    "lupin": {
        "roots": [
            "FOODON_00001206",  # lupin seed food product
            "FOODON_00002012",  # lupine bean food product
        ],
        "keywords": [
            "lupin",
            "lupine",
            "lupin bean",
            "lupini",
            "lupin flour",
        ],
    },
    "molluscs": {
        "roots": [
            "FOODON_00002044",  # mollusc food product
        ],
        "keywords": [
            "mollusc",
            "mollusk",
            "clam",
            "mussel",
            "oyster",
            "scallop",
            "squid",
            "octopus",
            "cuttlefish",
            "whelk",
            "cockle",
            "abalone",
            "snail",
        ],
    },
}

MILK_PLANT_EXCLUSION_REGEXES = [
    r".*\b(coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"([ -]+(flavoured|flavored))?[ -]+(milk|cream|yogurt|yoghurt)\b.*",
    r".*\b(milk|cream|yogurt|yoghurt)[ -]+alternative\b.*",
    r".*\bnon[ -]*dairy\b.*",
    r".*\bdairy[ -]*free\b.*",
    r".*\bplant[ -]*based\b.*",
    r".*\bvegan\b.*",
    r".*\b(peanut|almond|cashew|hazelnut|walnut|seed|nut)[ -]+butter\b.*",
    r".*\bbutter[ -]*beans?\b.*",
    r".*\bbeans?,[ -]*butter\b.*",
    r".*\bbutternut\b.*",
    r".*\bcream[ -]+substitute\b.*",
    # Known lossy canonical forms produced from non-dairy source phrases.
    r"^(powdered butter|cream rice|cream parsley|milk rice|coconut paste milk"
    r"|butter almond|oil cocoa butter|butter paper)$",
    r"^cream sherry$",
]

GLUTEN_SAFE_REGEXES = [
    r".*\bgluten[ -]*free\b.*",
    # HealthyFoods canonicalization removed "free" from these source terms.
    r"^gluten[ -]+(baking flour|self raising flour|flour|soy sauce|bread|"
    r"pasta|flour almond coconut|flour mix|bread mix)$",
    r".*\bbuckwheat\b.*",
    r".*\b(rice|tapioca|potato|almond|coconut|besan|chickpea|corn|maize|"
    r"quinoa|cassava|arrowroot)[ -]+flour\b.*",
    r".*\b(rice|pulse|chickpea|corn|maize|quinoa)[ -]+"
    r"(noodles?|pasta|spaghetti)\b.*",
    r".*\btamari\b.*",
    r"^(ground|minced|fresh|crystallized|crystallised|pickled|glace)?"
    r"[ -]*ginger$",
    r".*\b(wine|vinegar|vinaigrette)\b.*",
]

ALLERGEN_EXCLUSION_REGEXES = {
    "milk": MILK_PLANT_EXCLUSION_REGEXES,
    "gluten": GLUTEN_SAFE_REGEXES,
    "wheat": GLUTEN_SAFE_REGEXES,
}


def _keyword_regex(keyword: str) -> str:
    escaped = re.escape(keyword.strip().casefold()).replace(r"\ ", r"\s+")
    return rf".*\b{escaped}(e?s)?\b.*"


def _connect(uri: str, username: str, password: Optional[str], no_auth: bool):
    if no_auth:
        return GraphDatabase.driver(uri, auth=None)
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD or use --no-auth if allowed."
        )
    return GraphDatabase.driver(uri, auth=(username, password))


def _ensure_constraints(driver) -> None:
    statement = (
        "CREATE CONSTRAINT allergen_name IF NOT EXISTS "
        "FOR (n:Allergen) REQUIRE n.name IS UNIQUE"
    )
    with driver.session() as session:
        session.run(statement)


def _tag_by_foodon(driver, allergen_name: str, roots: list[str]) -> int:
    query = """
    MATCH (i:Ingredient)-[:HAS_CLASS]->(f:FoodOnClass)
    MATCH (f)-[:SUBCLASS_OF*0..]->(a:FoodOnClass)
    WHERE a.foodon_id IN $roots
      AND none(pattern IN $name_exclusions
               WHERE toLower(i.name) =~ pattern)
      AND (
        $allergen_name <> 'milk'
        OR (
          none(pattern IN $milk_exclusions
               WHERE toLower(i.name) =~ pattern)
          AND NOT EXISTS {
            MATCH (i)-[:HAS_CLASS]->(plant_class:FoodOnClass)
            MATCH (plant_class)-[:SUBCLASS_OF*0..]->(
              plant_root:FoodOnClass {foodon_id: 'FOODON_00001015'}
            )
          }
        )
      )
    WITH i,
         collect(distinct a.foodon_id) AS foodon_ids,
         collect(distinct a.label) AS foodon_labels
    MERGE (al:Allergen {name: $allergen_name})
    MERGE (i)-[r:HAS_ALLERGEN]->(al)
    SET r.sources = CASE
            WHEN r.sources IS NULL THEN ["foodon"]
            WHEN "foodon" IN r.sources THEN r.sources
            ELSE r.sources + ["foodon"]
        END,
        r.foodon_ids = foodon_ids,
        r.foodon_labels = foodon_labels
    RETURN count(distinct i) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            allergen_name=allergen_name,
            roots=roots,
            milk_exclusions=MILK_PLANT_EXCLUSION_REGEXES,
            name_exclusions=ALLERGEN_EXCLUSION_REGEXES.get(
                allergen_name, []
            ),
        )
        return int(result.single()["tagged"])


def _tag_by_keyword(driver, allergen_name: str, keywords: list[str]) -> int:
    keywords = [k.strip().casefold() for k in keywords if k.strip()]
    keyword_regexes = [_keyword_regex(keyword) for keyword in keywords]
    query = """
    MATCH (i:Ingredient)
    WHERE i.name IS NOT NULL
      AND (
        ($allergen_name IN ['milk', 'gluten', 'wheat']
         AND any(pattern IN $keyword_regexes
                 WHERE toLower(i.name) =~ pattern))
        OR
        (NOT $allergen_name IN ['milk', 'gluten', 'wheat']
         AND any(pattern IN $keyword_regexes
                 WHERE toLower(i.name) =~ pattern))
      )
      AND none(pattern IN $name_exclusions
               WHERE toLower(i.name) =~ pattern)
      AND (
        $allergen_name <> 'milk'
        OR (
          none(pattern IN $milk_exclusions
               WHERE toLower(i.name) =~ pattern)
          AND NOT EXISTS {
            MATCH (i)-[:HAS_CLASS]->(plant_class:FoodOnClass)
            MATCH (plant_class)-[:SUBCLASS_OF*0..]->(
              plant_root:FoodOnClass {foodon_id: 'FOODON_00001015'}
            )
          }
        )
      )
    WITH i, [idx IN range(0, size($keywords) - 1)
             WHERE (
               ($allergen_name IN ['milk', 'gluten', 'wheat']
                AND toLower(i.name) =~ $keyword_regexes[idx])
               OR
               (NOT $allergen_name IN ['milk', 'gluten', 'wheat']
                AND toLower(i.name) =~ $keyword_regexes[idx])
             ) |
             $keywords[idx]] AS hits
    MERGE (al:Allergen {name: $allergen_name})
    MERGE (i)-[r:HAS_ALLERGEN]->(al)
    SET r.sources = CASE
            WHEN r.sources IS NULL THEN ["keyword"]
            WHEN "keyword" IN r.sources THEN r.sources
            ELSE r.sources + ["keyword"]
        END,
        r.keyword_matches = CASE
            WHEN r.keyword_matches IS NULL THEN hits
            ELSE r.keyword_matches + [x IN hits WHERE NOT x IN r.keyword_matches]
        END
    RETURN count(distinct i) AS tagged
    """
    with driver.session() as session:
        result = session.run(
            query,
            allergen_name=allergen_name,
            keywords=keywords,
            keyword_regexes=keyword_regexes,
            milk_exclusions=MILK_PLANT_EXCLUSION_REGEXES,
            name_exclusions=ALLERGEN_EXCLUSION_REGEXES.get(
                allergen_name, []
            ),
        )
        return int(result.single()["tagged"])


def _clear_allergen_edges(driver, allergen_names: set[str]) -> int:
    query = """
    MATCH (:Ingredient)-[r:HAS_ALLERGEN]->(a:Allergen)
    WHERE a.name IN $allergen_names
    DELETE r
    RETURN count(r) AS deleted
    """
    with driver.session() as session:
        result = session.run(query, allergen_names=sorted(allergen_names))
        return int(result.single()["deleted"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tag Neo4j ingredients with supported allergen groups."
    )
    parser.add_argument(
        "--allergens",
        nargs="+",
        choices=sorted(ALLERGENS),
        help="Only backfill the selected allergen labels (default: all).",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Delete existing HAS_ALLERGEN edges for the selected allergens "
            "before rebuilding them."
        ),
    )
    args = parser.parse_args()

    if load_dotenv:
        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / ".env")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    no_auth = os.getenv("NEO4J_NO_AUTH") == "1"

    driver = _connect(uri, username, password, no_auth)
    try:
        _ensure_constraints(driver)
        selected = set(args.allergens or ALLERGENS)
        if args.replace:
            deleted = _clear_allergen_edges(driver, selected)
            print(f"deleted existing edges: {deleted}")
        items = [
            (name, config)
            for name, config in ALLERGENS.items()
            if name in selected
        ]
        iterator = tqdm(items, desc="Tagging allergens") if tqdm else items
        for allergen_name, config in iterator:
            foodon_tagged = _tag_by_foodon(
                driver, allergen_name, config["roots"]
            )
            keyword_tagged = _tag_by_keyword(
                driver, allergen_name, config["keywords"]
            )
            print(f"{allergen_name}: foodon={foodon_tagged}, keyword={keyword_tagged}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
