"""Neo4j repository adapter for recipe reads and writes."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.utils.neo4j_utils import driver, run_query


def fetch_recipe_scores_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id,
           r.nutriscore AS nutri_score,
           r.totalsustainabilityperserving AS sust_score,
           r.duration AS duration,
           r.serves AS serves,
           r.source AS source,
           r.title AS title
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): record
        for record in rows
        if record.get("recipe_id") is not None
    }


def fetch_recipe_image_urls_by_ids(ids: list[str]) -> dict[str, str | None]:
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id, r.image_url AS image_url
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): record.get("image_url")
        for record in rows
        if record.get("recipe_id") is not None
    }


def update_recipe_in_neo4j(
    recipe_id: str,
    instructions: list[str] | None = None,
    image_url: str | None = None,
) -> bool:
    """Patch mutable fields on an existing Recipe node. Returns False if not found."""
    if instructions is None and image_url is None:
        return True  # nothing to do

    set_clauses = []
    params: dict[str, Any] = {"recipe_id": recipe_id}

    if instructions is not None:
        set_clauses.append("r.instructions = $instructions")
        params["instructions"] = instructions
    if image_url is not None:
        set_clauses.append("r.image_url = $image_url")
        params["image_url"] = image_url
    set_clauses.append("r.edited = true")
    set_clauses.append("r.edited_at = datetime()")

    result = run_query(
        f"MATCH (r:Recipe {{recipe_id: $recipe_id}}) SET {', '.join(set_clauses)} RETURN r.recipe_id AS rid",
        params,
    )
    return bool(result)


def upsert_recipe_to_neo4j(
    recipe_id: str,
    title: str,
    ingredient_lines: list[str],
    ingredient_names: list[str],
    measurements: list[str],
    instructions: list[str],
    duration: float,
    serves: float,
    image_url: str | None,
    allergens: list[str],
    tags: list[str],
    source: str = "user",
) -> None:
    """Write (or update) a recipe and its ingredient/allergen/tag graph in Neo4j.

    Args:
        ingredient_lines: Original raw strings ("1 cup flour") — stored on Ingredients_original.
        ingredient_names:  Clean names ("flour") — stored on Ingredient nodes.
        measurements:      Quantity+unit strings ("1 cup") — stored on HAS_INGREDIENT edge.
    """
    with driver.session() as session:
        # 1. Upsert the Recipe node
        session.run(
            """
            MERGE (r:Recipe {recipe_id: $recipe_id})
            SET r.title       = $title,
                r.source      = $source,
                r.status      = 'active',
                r.duration    = $duration,
                r.serves      = $serves,
                r.image_url   = $image_url,
                r.instructions = $instructions,
                r.edited      = coalesce(r.edited, false)
            """,
            {
                "recipe_id": recipe_id,
                "title": title,
                "source": source,
                "duration": duration,
                "serves": serves,
                "image_url": image_url,
                "instructions": instructions,
            },
        )

        # 2. Upsert each ingredient
        for position, (line, name, measurement) in enumerate(
            zip(ingredient_lines, ingredient_names, measurements)
        ):
            session.run(
                """
                MATCH (r:Recipe {recipe_id: $recipe_id})

                MERGE (o:Ingredients_original {original_id: $original_id})
                SET o.name         = $line,
                    o.original_text = $line,
                    o.source       = $source,
                    o.status       = 'active'
                MERGE (r)-[:HAS_INGREDIENT_ORIGINAL {position: $position}]->(o)

                MERGE (i:Ingredient {name: $name})
                ON CREATE SET
                    i.canonical_id = randomUUID(),
                    i.source       = $source,
                    i.status       = 'resolved'
                ON MATCH SET
                    i.canonical_id = coalesce(i.canonical_id, randomUUID()),
                    i.source       = coalesce(i.source, $source),
                    i.status       = coalesce(i.status, 'resolved')

                MERGE (o)-[:MAPS_TO]->(i)
                MERGE (r)-[hi:HAS_INGREDIENT]->(i)
                ON CREATE SET hi.measurement = $measurement, hi.unit = null
                """,
                {
                    "recipe_id": recipe_id,
                    "original_id": f"{recipe_id}:{position}",
                    "position": position,
                    "line": line,
                    "name": name,
                    "measurement": measurement,
                    "source": source,
                },
            )

        # 3. Tag ingredients with allergens (shared Ingredient nodes, affects all recipes using them)
        for allergen in allergens:
            session.run(
                """
                MATCH (r:Recipe {recipe_id: $recipe_id})-[:HAS_INGREDIENT]->(i:Ingredient)
                WHERE any(k IN $keywords WHERE toLower(i.name) CONTAINS k)
                MERGE (al:Allergen {name: $allergen})
                MERGE (i)-[:HAS_ALLERGEN]->(al)
                """,
                {
                    "recipe_id": recipe_id,
                    "allergen": allergen,
                    "keywords": _ALLERGEN_KEYWORDS.get(allergen, [allergen]),
                },
            )

        # 4. Add diet tags on the Recipe node
        for tag in tags:
            session.run(
                """
                MATCH (r:Recipe {recipe_id: $recipe_id})
                MERGE (t:Tag {name: $tag})
                ON CREATE SET t.category = 'dietary'
                MERGE (r)-[:HAS_TAG]->(t)
                """,
                {"recipe_id": recipe_id, "tag": tag},
            )


# ---------------------------------------------------------------------------
# Allergen keyword map (mirrors scripts/neo4j/tag_allergens.py)
# ---------------------------------------------------------------------------

_ALLERGEN_KEYWORDS: dict[str, list[str]] = {
    "milk": ["milk", "cheese", "butter", "cream", "yogurt", "whey", "casein", "lactose", "ghee", "curd", "kefir"],
    "egg": ["egg", "egg white", "egg yolk", "omelet", "mayonnaise", "aioli", "meringue", "albumen"],
    "peanut": ["peanut", "peanut butter", "groundnut", "arachis"],
    "tree_nut": ["almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut", "macadamia", "brazil nut", "pine nut"],
    "wheat": ["wheat", "whole wheat", "durum", "semolina", "spelt", "farro", "flour", "bread", "pasta", "noodle"],
    "soy": ["soy", "soya", "soybean", "tofu", "miso", "tempeh", "edamame"],
    "fish": ["fish", "cod", "bass", "tuna", "salmon", "tilapia", "halibut", "trout", "sardine", "anchovy"],
    "crustacean_shellfish": ["crab", "lobster", "shrimp", "prawn", "crawfish", "crayfish", "scallop", "clam", "oyster", "mussel"],
    "sesame": ["sesame", "tahini", "sesame oil", "sesame seed"],
    "gluten": ["gluten", "wheat", "barley", "rye", "malt", "brewer"],
}


def detect_allergens_from_names(ingredient_names: list[str]) -> list[str]:
    """Return allergen labels present in the ingredient name list (keyword match)."""
    found: set[str] = set()
    for name in ingredient_names:
        name_lower = name.strip().lower()
        for allergen, keywords in _ALLERGEN_KEYWORDS.items():
            if any(kw in name_lower for kw in keywords):
                found.add(allergen)
    return sorted(found)


def infer_diet_tags(allergens: set[str]) -> list[str]:
    """Derive diet tags deterministically from the allergen set."""
    tags: list[str] = []
    if "milk" not in allergens:
        tags.append("dairy_free")
    if "peanut" not in allergens and "tree_nut" not in allergens:
        tags.append("nut_free")
    if "gluten" not in allergens and "wheat" not in allergens:
        tags.append("gluten_free")
    if not allergens.intersection({"fish", "crustacean_shellfish"}):
        tags.append("pescatarian_safe")
    return tags
