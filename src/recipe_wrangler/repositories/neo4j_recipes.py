"""Neo4j repository adapter for recipe reads and writes."""

from __future__ import annotations

import logging
import re
from typing import Any

from recipe_wrangler.utils.neo4j_utils import driver, run_query
from recipe_wrangler.utils.recipe_status import NEO4J_NOT_DISABLED, STATUS_DISABLED

logger = logging.getLogger(__name__)

_STATUS_BATCH_SIZE = 5000


SOURCE_COLLECTION_IDS: dict[str, str] = {
    "recipe1m": "urn:rcollection:recipe1m",
    "HealthyFoods": "urn:rcollection:healthyfood",
    "FoodHero": "urn:rcollection:foodhero",
    "Irish_SafeFood": "urn:rcollection:rcsi-recipes",
    "Curated Irish Recipes": "urn:rcollection:rcsi-recipes",
    "MyPlate": "urn:rcollection:myplate",
}


def resolve_collection_source_id(source: str, source_id: str | None = None) -> str | None:
    mapped = SOURCE_COLLECTION_IDS.get(str(source))
    if mapped is not None:
        return mapped
    if source_id is not None and str(source_id).strip():
        return source_id
    source_text = str(source).strip()
    return source_text or None

def count_recipes() -> int:
    rows = run_query(f"MATCH (r:Recipe) WHERE {NEO4J_NOT_DISABLED} RETURN count(r) AS total")
    return int(rows[0]["total"]) if rows else 0


def set_recipe_status(
    recipe_ids: list[str],
    status: str,
    reason: str | None = None,
) -> list[str]:
    """Set `status` on recipes by ID (matches recipe_id or id). Returns the
    resolved canonical recipe_ids that were updated — the caller needs them
    for ES sync and cache invalidation, and their count is the affected count.

    Disabling stamps disabled_at/disabled_reason; enabling clears them.
    """
    ids = list(dict.fromkeys(str(rid).strip() for rid in recipe_ids if str(rid).strip()))
    if not ids:
        return []

    # Equality on the raw property (no toString wrapper) so the planner can use
    # the recipe_id/id indexes — wrapping degrades every batch to label scans.
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    SET r.status = $status,
        r.disabled_at = (CASE WHEN $status = $disabled THEN datetime() ELSE null END),
        r.disabled_reason = (CASE WHEN $status = $disabled THEN $reason ELSE null END)
    RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id
    """
    updated: list[str] = []
    for start in range(0, len(ids), _STATUS_BATCH_SIZE):
        rows = run_query(query, {
            "ids": ids[start:start + _STATUS_BATCH_SIZE],
            "status": status,
            "disabled": STATUS_DISABLED,
            "reason": reason,
        })
        updated.extend(str(row["recipe_id"]) for row in rows if row.get("recipe_id"))
    return updated


def resolve_recipe_ids_by_query(where_clause: str, params: dict[str, Any]) -> list[str]:
    """Resolve every recipe ID matching a param_search WHERE clause.

    Single pass on purpose: SKIP/LIMIT paging re-runs the full facet match
    (and a sort on a computed value) once per page, and the caller holds the
    complete ID list in memory anyway.
    """
    query = f"""
    MATCH (r:Recipe)
    {where_clause}
    RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id
    """
    rows = run_query(query, {k: v for k, v in params.items() if k not in ("limit", "offset")})
    return list(dict.fromkeys(str(row["recipe_id"]) for row in rows if row.get("recipe_id")))


def fetch_recipe_scores_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    RETURN rid AS recipe_id,
           coalesce(r.nutriscore, null) AS nutri_score,
           coalesce(r.totalsustainabilityperserving, null) AS sust_score,
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


def fetch_recipe_dish_types_by_ids(ids: list[str]) -> dict[str, list[str]]:
    """Return a mapping of recipe_id -> list of dish-type tag names."""
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    OPTIONAL MATCH (r)-[:HAS_TAG]->(dt:Tag)
      WHERE dt.category = 'dish-type'
    WITH rid, [n IN collect(DISTINCT dt.name) WHERE n IS NOT NULL AND trim(toString(n)) <> ""] AS dish_types
    RETURN rid AS recipe_id, dish_types
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): list(record.get("dish_types") or [])
        for record in rows
        if record.get("recipe_id") is not None
    }


def fetch_recipe_allergens_by_ids(ids: list[str]) -> dict[str, list[str]]:
    """Return a mapping of recipe_id -> distinct allergen names (via ingredients)."""
    if not ids:
        return {}
    query = """
    UNWIND $ids AS rid
    MATCH (r:Recipe)
    WHERE r.recipe_id = rid OR r.id = rid
    OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(:Ingredient)-[:HAS_ALLERGEN]->(al:Allergen)
    WITH rid, [n IN collect(DISTINCT al.name) WHERE n IS NOT NULL AND trim(toString(n)) <> ""] AS allergens
    RETURN rid AS recipe_id, allergens
    """
    rows = run_query(query, {"ids": ids})
    return {
        str(record.get("recipe_id")): list(record.get("allergens") or [])
        for record in rows
        if record.get("recipe_id") is not None
    }


def update_recipe_in_neo4j(
    recipe_id: str,
    instructions: list[str] | None = None,
    image_url: str | None = None,
    source_id: str | None = None,
    expert_recipe: bool | None = None,
    title: str | None = None,
    allergens: list[str] | None = None,
    tags: list[str] | None = None,
    duration: float | None = None,
) -> bool:
    """Patch mutable fields on an existing Recipe node. Returns False if not found."""
    if all(v is None for v in [instructions, image_url, source_id, expert_recipe, title, allergens, tags, duration]):
        return True  # nothing to do

    set_clauses = []
    params: dict[str, Any] = {"recipe_id": recipe_id}

    if instructions is not None:
        set_clauses.append("r.instructions = $instructions")
        params["instructions"] = instructions
    if image_url is not None:
        set_clauses.append("r.image_url = $image_url")
        params["image_url"] = image_url
    if source_id is not None:
        set_clauses.append("r.source_id = $source_id")
        params["source_id"] = source_id
    if expert_recipe is not None:
        set_clauses.append("r.expert_recipe = $expert_recipe")
        params["expert_recipe"] = expert_recipe
    if title is not None:
        set_clauses.append("r.title = $title")
        params["title"] = title
    if duration is not None:
        set_clauses.append("r.duration = $duration")
        params["duration"] = duration
    set_clauses.append("r.edited = true")
    set_clauses.append("r.edited_at = datetime()")

    result = run_query(
        f"MATCH (r:Recipe) WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id SET {', '.join(set_clauses)} RETURN coalesce(r.recipe_id, r.id) AS rid",
        params,
    )
    if not result:
        return False

    if allergens is not None:
        with driver.session() as session:
            # Remove all existing allergen edges from this recipe's ingredients, then re-add
            session.run(
                """
                MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)-[rel:HAS_ALLERGEN]->(:Allergen)
                WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
                DELETE rel
                """,
                {"recipe_id": recipe_id},
            )
            for allergen in allergens:
                session.run(
                    """
                    MATCH (r:Recipe)-[:HAS_INGREDIENT]->(i:Ingredient)
                    WHERE (r.recipe_id = $recipe_id OR r.id = $recipe_id)
                      AND any(k IN $keywords WHERE toLower(i.name) CONTAINS k)
                    MERGE (al:Allergen {name: $allergen})
                    MERGE (i)-[:HAS_ALLERGEN]->(al)
                    """,
                    {
                        "recipe_id": recipe_id,
                        "allergen": allergen,
                        "keywords": _ALLERGEN_KEYWORDS.get(allergen, [allergen]),
                    },
                )

    if tags is not None:
        with driver.session() as session:
            session.run(
                """
                MATCH (r:Recipe)-[rel:HAS_TAG]->(:Tag)
                WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
                DELETE rel
                """,
                {"recipe_id": recipe_id},
            )
            for tag in tags:
                session.run(
                    """
                    MATCH (r:Recipe)
                    WHERE r.recipe_id = $recipe_id OR r.id = $recipe_id
                    MERGE (t:Tag {name: $tag})
                    MERGE (r)-[:HAS_TAG]->(t)
                    """,
                    {"recipe_id": recipe_id, "tag": tag},
                )

    return True


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
    source_id: str | None = None,
    expert_recipe: bool = False,
) -> None:
    """Write (or update) a recipe and its ingredient/allergen/tag graph in Neo4j.

    Args:
        ingredient_lines: Original raw strings ("1 cup flour") — stored on Ingredients_original.
        ingredient_names:  Clean names ("flour") — stored on Ingredient nodes.
        measurements:      Quantity+unit strings ("1 cup") — stored on HAS_INGREDIENT edge.
    """
    source_id = resolve_collection_source_id(source, source_id)

    with driver.session() as session:
        # 1. Upsert the Recipe node
        session.run(
            """
            MERGE (r:Recipe {recipe_id: $recipe_id})
            SET r.title         = $title,
                r.source        = $source,
                r.source_id     = $source_id,
                r.expert_recipe = $expert_recipe,
                r.status        = 'active',
                r.duration      = $duration,
                r.serves        = $serves,
                r.image_url     = $image_url,
                r.instructions  = $instructions,
                r.edited        = coalesce(r.edited, false)
            """,
            {
                "recipe_id": recipe_id,
                "title": title,
                "source": source,
                "source_id": source_id,
                "expert_recipe": expert_recipe,
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
    "crustacean_shellfish": ["crab", "lobster", "shrimp", "prawn", "crawfish", "crayfish", "crustacean", "langostino"],
    "sesame": ["sesame", "tahini", "sesame oil", "sesame seed"],
    "gluten": ["gluten", "wheat", "barley", "rye", "malt", "brewer"],
    "celery": ["celery", "celeriac", "celery seed", "celery salt"],
    "mustard": ["mustard", "mustard seed", "mustard powder", "mustard flour"],
    "sulphites": [
        "sulphite", "sulphites", "sulfite", "sulfites",
        "sulphur dioxide", "sulfur dioxide",
        "metabisulphite", "metabisulfite", "bisulphite", "bisulfite",
        "sodium sulphite", "sodium sulfite", "potassium sulphite", "potassium sulfite",
        "e220", "e221", "e222", "e223", "e224", "e225", "e226", "e227", "e228",
    ],
    "lupin": ["lupin", "lupine", "lupin bean", "lupini", "lupin flour"],
    "molluscs": [
        "mollusc", "mollusk", "clam", "mussel", "oyster", "scallop",
        "squid", "octopus", "cuttlefish", "whelk", "cockle", "abalone", "snail",
    ],
}

_PLANT_DAIRY_ALTERNATIVE_PATTERNS = (
    r"\b(?:coconut|soy|soya|almond|oat|rice|cashew|hazelnut|hemp|pea)"
    r"(?:[\s-]+(?:flavoured|flavored))?[\s-]+(?:milk|cream|yogurt|yoghurt)\b",
    r"\b(?:milk|cream|yogurt|yoghurt)[\s-]+alternative\b",
    r"\bnon[\s-]*dairy\b",
    r"\bdairy[\s-]*free\b",
    r"\bplant[\s-]*based\b",
    r"\bvegan\b",
    r"\b(?:peanut|almond|cashew|hazelnut|walnut|seed|nut)[\s-]+butter\b",
    r"\bbutter[\s-]*beans?\b",
    r"\bbeans?,[\s-]*butter\b",
    r"\bbutternut\b",
    r"\bcream[\s-]+substitute\b",
    r"^(?:powdered butter|cream rice|cream parsley|milk rice|"
    r"coconut paste milk|butter almond|oil cocoa butter|butter paper)$",
    r"^cream sherry$",
)

_GLUTEN_SAFE_PATTERNS = (
    r"\bgluten[\s-]*free\b",
    r"^gluten[\s-]+(?:baking flour|self raising flour|flour|soy sauce|bread|"
    r"pasta|flour almond coconut|flour mix|bread mix)$",
    r"\bbuckwheat\b",
    r"\b(?:rice|tapioca|potato|almond|coconut|besan|chickpea|corn|maize|"
    r"quinoa|cassava|arrowroot)[\s-]+flour\b",
    r"\b(?:rice|pulse|chickpea|corn|maize|quinoa)[\s-]+"
    r"(?:noodles?|pasta|spaghetti)\b",
    r"\btamari\b",
    r"^(?:ground|minced|fresh|crystallized|crystallised|pickled|glace)?"
    r"[\s-]*ginger$",
    r"\b(?:wine|vinegar|vinaigrette)\b",
)


def _is_plant_dairy_alternative(name: str) -> bool:
    normalized = re.sub(r"\s+", " ", name.strip().casefold())
    return any(
        re.search(pattern, normalized)
        for pattern in _PLANT_DAIRY_ALTERNATIVE_PATTERNS
    )


def _is_gluten_safe_name(name: str) -> bool:
    normalized = re.sub(r"\s+", " ", name.strip().casefold())
    return any(re.search(pattern, normalized) for pattern in _GLUTEN_SAFE_PATTERNS)


def _keyword_matches(name: str, keyword: str) -> bool:
    """Match an allergen keyword as words, not as an arbitrary substring."""
    pattern = (
        r"(?<!\w)"
        + re.escape(keyword.casefold()).replace(r"\ ", r"\s+")
        + r"(?:e?s)?(?!\w)"
    )
    return re.search(pattern, name.casefold()) is not None


def find_ingredient_substitutes(ingredient_name: str) -> dict[str, Any]:
    """Return substitution candidates for an ingredient.

    Lookup order:
      1. HAS_SUBSTITUTION edges (MISKG-curated, most reliable).
      2. FoodOn taxonomy siblings (3-hop ancestor, broader coverage).

    Returns a dict with keys:
      - ``candidates``: ordered list of substitute ingredient names (may be empty)
      - ``source``: "graph_direct" | "foodon_taxonomy" | None
    """
    # --- Priority 1a: MISKG HAS_SUBSTITUTION — exact name match ---
    direct_query = """
    MATCH (i:Ingredient)
    WHERE toLower(i.name) = toLower($name)
    MATCH (i)-[:HAS_SUBSTITUTION]->(sub:Ingredient)
    RETURN sub.name AS name
    LIMIT 10
    """
    rows = run_query(direct_query, {"name": ingredient_name})
    candidates = [r["name"] for r in rows if r.get("name")]
    if candidates:
        return {"candidates": candidates, "source": "graph_direct"}

    # --- Priority 1b: MISKG HAS_SUBSTITUTION — single-word-qualifier variant match ---
    # Catches true modifiers: "salted butter", "unsalted butter", "clarified butter".
    # Rejects compound ingredients like "unsweetened apple butter" or "creamy peanut butter"
    # by requiring the variant name to be exactly two words (one qualifier + the ingredient).
    variant_query = """
    MATCH (i:Ingredient)
    WHERE toLower(i.name) ENDS WITH (' ' + toLower($name))
      AND size(split(i.name, ' ')) = 2
    MATCH (i)-[:HAS_SUBSTITUTION]->(sub:Ingredient)
    WHERE toLower(sub.name) <> toLower($name)
    RETURN sub.name AS name, count(*) AS freq
    ORDER BY freq DESC
    LIMIT 10
    """
    rows = run_query(variant_query, {"name": ingredient_name})
    candidates = [r["name"] for r in rows if r.get("name")]
    if candidates:
        return {"candidates": candidates, "source": "graph_direct"}

    # --- Priority 2: FoodOn taxonomy — tightest fit first ---
    # Try 1 hop up, then 2, then 3. Stop at the first depth that yields
    # results so substitutes stay as close in the taxonomy as possible.
    taxonomy_query_tmpl = """
    MATCH (i:Ingredient)
    WHERE toLower(i.name) = toLower($name)
    MATCH (i)-[:HAS_CLASS]->(c:FoodOnClass)
    MATCH (c)-[:SUBCLASS_OF*{depth}]->(ancestor:FoodOnClass)
    MATCH (sib:FoodOnClass)-[:SUBCLASS_OF*1..{depth}]->(ancestor)
    WHERE sib <> c
    MATCH (cand:Ingredient)-[:HAS_CLASS]->(sib)
    WHERE toLower(cand.name) <> toLower($name)
    RETURN DISTINCT cand.name AS name
    LIMIT 10
    """
    for depth in [1, 2, 3]:
        query = taxonomy_query_tmpl.replace("{depth}", str(depth))
        rows = run_query(query, {"name": ingredient_name})
        candidates = [r["name"] for r in rows if r.get("name")]
        if candidates:
            return {"candidates": candidates, "source": "foodon_taxonomy"}

    return {"candidates": [], "source": None}


def detect_allergens_from_names(ingredient_names: list[str]) -> list[str]:
    """Return allergen labels present in the ingredient name list (keyword match)."""
    found: set[str] = set()
    for name in ingredient_names:
        name_lower = name.strip().lower()
        for allergen, keywords in _ALLERGEN_KEYWORDS.items():
            if allergen == "milk" and _is_plant_dairy_alternative(name_lower):
                continue
            if allergen in {"gluten", "wheat"} and _is_gluten_safe_name(
                name_lower
            ):
                continue
            matched = any(_keyword_matches(name_lower, kw) for kw in keywords)
            if matched:
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
    if not allergens.intersection({"fish", "crustacean_shellfish", "molluscs"}):
        tags.append("pescatarian_safe")
    return tags


def _normalize_foodchat_terms(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        term = str(item or "").strip().casefold()
        if not term:
            continue
        variants = [term]
        if term.endswith("ies") and len(term) > 3:
            variants.append(term[:-3] + "y")
        elif term.endswith("s") and len(term) > 1 and not term.endswith("ss"):
            variants.append(term[:-1])
        for variant in variants:
            if variant and variant not in seen:
                seen.add(variant)
                normalized.append(variant)
    return normalized


def _normalize_foodchat_tags(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        term = str(item or "").strip().casefold()
        if not term:
            continue
        spaced = term.replace("_", " ").replace("-", " ")
        variants = [
            term,
            spaced,
            spaced.replace(" ", "_"),
            spaced.replace(" ", "-"),
        ]
        for variant in variants:
            cleaned = variant.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
    return normalized


_SLOT_TO_DB_TAG: dict[str, str] = {
    "breakfast": "breakfast",
    "lunch": "main-dish",
    "dinner": "main-dish",
    "main-dish": "main-dish",
    "snack": "snacks",
    "snacks": "snacks",
    "dessert": "desserts",
    "desserts": "desserts",
    "beverage": "beverages",
    "beverages": "beverages",
}


_ES_POOL_MULTIPLIER = 10  # how many ES IDs to sample per quota unit before Neo4j filtering

# Sources considered high-quality — boosted in ES random sampling.
_TRUSTED_SOURCES = {"foodhero", "healthyfoods", "irish_safefood"}


def _es_sample_ids(exclude_ids: list[str], pool_size: int) -> list[str]:
    """Sample a random pool of candidate IDs from ES across all recipes.

    No dish-type filter here — ES tags only carry diet labels, not dish types.
    Neo4j handles dish-type filtering against the sampled pool.
    """
    from recipe_wrangler.api.config import get_settings
    from recipe_wrangler.utils.http_pool import get_http_session
    settings = get_settings()

    bool_query: dict = {"must": [{"match_all": {}}]}
    if exclude_ids:
        bool_query["must_not"] = [{"ids": {"values": exclude_ids}}]

    query: dict = {
        "function_score": {
            "query": {"bool": bool_query},
            "random_score": {},
            "boost_mode": "replace",
        }
    }

    payload = {"size": pool_size, "_source": ["id"], "query": query}
    url = f"{settings.elastic_url}/{settings.elastic_index}/_search"
    resp = get_http_session().post(url, json=payload, timeout=settings.elastic_timeout)
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    return [str(h.get("_source", {}).get("id") or h.get("_id") or "").strip() for h in hits if h]


def fetch_foodchat_candidates(request_data: Any) -> dict[str, list[dict]]:
    """Fetch recipe candidates for FoodChat.

    Neo4j scans by dish-type tag directly — ES has no dish-type tags so cannot
    pre-filter. Expensive steps (allergen taxonomy, diet-tag validation) are
    skipped when the caller passes no constraints. Postgres does a single batch
    nutrition fetch across all slots.
    """
    import time
    from recipe_wrangler.repositories.postgres_nutrition import get_recipe_nutrition_batch

    results: dict[str, list[dict]] = {}

    allergies = _normalize_foodchat_terms(request_data.user_profile.allergies)
    exclude_ingredients = _normalize_foodchat_terms(request_data.constraints.exclude_ingredients)
    combined_exclusions = list(dict.fromkeys(allergies + exclude_ingredients))
    include_ingredients = _normalize_foodchat_terms(request_data.constraints.include_ingredients)
    exclude_ids: list[str] = [
        str(rid).strip() for rid in request_data.constraints.exclude_recipe_ids if str(rid).strip()
    ]
    favorite_ids: list[str] = [
        str(rid).strip()
        for rid in getattr(request_data.constraints, "favorite_recipe_ids", [])
        if str(rid).strip()
    ]
    diet_tags = _normalize_foodchat_tags(request_data.user_profile.diet)
    nutrition_profile = request_data.constraints.nutrition_profile
    randomize: bool = getattr(request_data, "randomize", True)

    _NUTRITION_POOL_MULTIPLIER = 5

    has_diet = bool(diet_tags)
    has_exclusions = bool(combined_exclusions)
    has_inclusions = bool(include_ingredients)
    has_favorites = bool(favorite_ids)

    if has_diet:
        diet_validation = """
    OPTIONAL MATCH (valid_tag:Tag)
    WHERE toLower(valid_tag.name) IN $diet_tags
    WITH collect(toLower(valid_tag.name)) AS valid_diet_tags
    """
        diet_tag_match = """
    OPTIONAL MATCH (r)-[:HAS_TAG]->(t:Tag)
    WITH r, valid_diet_tags, collect(toLower(t.name)) AS recipe_tags
    WHERE ALL(d IN valid_diet_tags WHERE d IN recipe_tags)
    """
    else:
        diet_validation = "WITH [] AS valid_diet_tags"
        diet_tag_match = ""

    if has_exclusions:
        exclusion_filter = """
    OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
    WITH r, collect(i) AS ingredients
    WHERE NOT ANY(i IN ingredients WHERE
        ANY(ex IN $combined_exclusions WHERE toLower(i.name) CONTAINS ex)
        OR EXISTS {
            MATCH (i)-[:HAS_CLASS]->(:FoodOnClass)-[:SUBCLASS_OF*0..5]->(ancestor:FoodOnClass)
            WHERE ANY(ex IN $combined_exclusions WHERE toLower(ancestor.name) CONTAINS ex)
        }
    )
    WITH r, ingredients
    """
    else:
        exclusion_filter = "WITH r, [] AS ingredients"

    inclusion_score = (
        "size([i IN ingredients WHERE ANY(inc IN $include_ingredients WHERE toLower(i.name) CONTAINS inc)])"
        if has_inclusions else "0"
    )

    # Soft ranking boost for favorited recipes. Weight 10 vs 1 per include_ingredient
    # hit, so a favorite clearly outranks a single ingredient match. Computed after
    # the diet/allergen/exclusion filters, so a favorite that violates a hard
    # constraint never appears; exclude_recipe_ids also still removes favorites.
    favorite_boost = (
        "CASE WHEN coalesce(toString(r.recipe_id), toString(r.id), '') IN $favorite_recipe_ids"
        " THEN 10 ELSE 0 END"
        if has_favorites else "0"
    )

    neo4j_query = f"""
    {diet_validation}

    MATCH (r:Recipe)-[:HAS_TAG]->(dt:Tag)
    WHERE dt.category = 'dish-type' AND toLower(dt.name) = $dish_type
      AND r.instructions IS NOT NULL
      AND size(r.instructions) > 0
      AND toLower(coalesce(r.source, '')) <> 'recipe1m'
      AND {NEO4J_NOT_DISABLED}
      AND (size($exclude_ids) = 0 OR NOT coalesce(toString(r.recipe_id), toString(r.id), '') IN $exclude_ids)

    WITH r

    {diet_tag_match}

    {exclusion_filter}

    WITH r, {inclusion_score} AS include_score, {favorite_boost} AS favorite_boost

    ORDER BY CASE WHEN $randomize THEN favorite_boost ELSE favorite_boost + include_score END DESC,
             CASE WHEN $randomize THEN rand() ELSE (1.0 - include_score) END ASC
    LIMIT $fetch_limit

    OPTIONAL MATCH (r)-[:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original)
    WITH r, collect(o.name) AS orig_ingredients

    RETURN coalesce(toString(r.recipe_id), toString(r.id)) AS recipe_id,
           r.title AS title,
           reduce(s = '', x IN orig_ingredients | CASE WHEN s = '' THEN x ELSE s + ', ' + x END) AS ingredients,
           reduce(s = '', x IN coalesce(r.instructions, []) | CASE WHEN s = '' THEN x ELSE s + ' ' + x END) AS directions
    """

    quotas = {k: max(0, int(v)) for k, v in request_data.quotas.items()}
    for slot in quotas:
        if quotas[slot] == 0:
            results[slot] = []
    if not any(quotas.values()):
        return results

    all_candidates: dict[str, list[dict]] = {}
    all_recipe_ids: list[str] = []
    running_exclude = list(exclude_ids)

    base_params = {
        "diet_tags": diet_tags,
        "combined_exclusions": combined_exclusions,
        "include_ingredients": include_ingredients,
        "favorite_recipe_ids": favorite_ids,
        "randomize": randomize,
    }

    for slot, quota in quotas.items():
        if quota == 0:
            all_candidates[slot] = []
            continue

        db_tag = _SLOT_TO_DB_TAG.get(slot.strip().casefold(), slot.strip().casefold())
        fetch_limit = quota * _NUTRITION_POOL_MULTIPLIER if nutrition_profile else quota

        t0 = time.monotonic()
        rows = run_query(neo4j_query, {
            **base_params,
            "dish_type": db_tag,
            "fetch_limit": fetch_limit,
            "exclude_ids": running_exclude,
        })
        logger.info("Neo4j slot=%s db_tag=%s rows=%d in %.2fs", slot, db_tag, len(rows), time.monotonic() - t0)

        candidates: list[dict] = []
        for row in rows:
            recipe_id = str(row.get("recipe_id") or "").strip()
            title = str(row.get("title") or "").strip()
            if not recipe_id or not title:
                continue
            candidates.append({
                "recipe_id": recipe_id,
                "title": title,
                "ingredients": str(row.get("ingredients") or ""),
                "directions": str(row.get("directions") or ""),
                "dish_type": slot,
            })
            all_recipe_ids.append(recipe_id)

        running_exclude = running_exclude + [c["recipe_id"] for c in candidates]
        all_candidates[slot] = candidates

    nutrition_map: dict = {}
    if all_recipe_ids:
        t2 = time.monotonic()
        nutrition_map = get_recipe_nutrition_batch(all_recipe_ids)
        logger.info("Postgres nutrition batch ids=%d in %.2fs", len(all_recipe_ids), time.monotonic() - t2)
        for slot_candidates in all_candidates.values():
            for c in slot_candidates:
                c["_nutrition_raw"] = nutrition_map.get(c["recipe_id"])

    def _passes_nutrition(c: dict) -> bool:
        raw = c.get("_nutrition_raw")
        if not raw:
            return True
        nutrients = raw.get("total_nutrients_per_serving") or raw.get("total_nutrients") or {}
        if not isinstance(nutrients, dict):
            return True

        def _val(*keys: str) -> float | None:
            for k in keys:
                v = nutrients.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None

        np = nutrition_profile
        kcal = _val("energy_kcal", "calories")
        protein = _val("protein_g")
        carbs = _val("carbohydrate_g", "carbs_g")
        fat = _val("fat_g")
        if np.min_calories is not None and kcal is not None and kcal < np.min_calories:
            return False
        if np.max_calories is not None and kcal is not None and kcal > np.max_calories:
            return False
        if np.min_protein_g is not None and protein is not None and protein < np.min_protein_g:
            return False
        if np.max_protein_g is not None and protein is not None and protein > np.max_protein_g:
            return False
        if np.min_carbs_g is not None and carbs is not None and carbs < np.min_carbs_g:
            return False
        if np.max_carbs_g is not None and carbs is not None and carbs > np.max_carbs_g:
            return False
        if np.min_fat_g is not None and fat is not None and fat < np.min_fat_g:
            return False
        if np.max_fat_g is not None and fat is not None and fat > np.max_fat_g:
            return False
        return True

    for slot, candidates in all_candidates.items():
        safe_quota = quotas[slot]

        if nutrition_profile:
            candidates = [c for c in candidates if _passes_nutrition(c)]

        slot_results: list[dict] = []
        for c in candidates[:safe_quota]:
            raw = c.pop("_nutrition_raw", None)
            nutrition_out: dict | None = None
            if raw:
                nutrients = raw.get("total_nutrients_per_serving") or raw.get("total_nutrients") or {}
                if isinstance(nutrients, dict):
                    def _pick(*keys: str) -> float | None:
                        for k in keys:
                            v = nutrients.get(k)
                            if v is not None:
                                try:
                                    return float(v)
                                except (TypeError, ValueError):
                                    pass
                        return None
                    nutrition_out = {
                        "calories": _pick("energy_kcal", "calories"),
                        "protein_g": _pick("protein_g"),
                        "carbs_g": _pick("carbohydrate_g", "carbs_g"),
                        "fat_g": _pick("fat_g"),
                    }
            c["nutrition"] = nutrition_out
            slot_results.append(c)

        results[slot] = slot_results

    return results
