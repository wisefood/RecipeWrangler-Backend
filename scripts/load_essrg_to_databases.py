"""
Step 1.7: Load ESSRG Recipes into Runtime Databases

Loads recipes into:
  1. Neo4j: Recipe nodes + ingredient relationships
  2. PostgreSQL: Recipe profiles and nutrition metadata
  3. Elasticsearch: Searchable recipe index

This script prepares exports and optionally commits to live databases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# ── config ────────────────────────────────────────────────────────────────────
PROFILES_JSON = Path("output/ESSRG/recipes_with_profiles.json")
RECIPES_JSON = Path("data/ESSRG/ESSRG_recipes_clean.json")
OUTPUT_DIR = Path("output/ESSRG")

NEO4J_RECIPES_CSV = OUTPUT_DIR / "neo4j_recipes_import.csv"
NEO4J_INGREDIENTS_CSV = OUTPUT_DIR / "neo4j_ingredients_import.csv"
NEO4J_RELATIONSHIPS_CSV = OUTPUT_DIR / "neo4j_recipe_ingredient_rels.csv"

ELASTICSEARCH_CSV = OUTPUT_DIR / "elasticsearch_recipes_import.csv"
POSTGRES_RECIPES_CSV = OUTPUT_DIR / "postgres_recipes_import.csv"
POSTGRES_PROFILES_CSV = OUTPUT_DIR / "postgres_profiles_import.csv"

SOURCE = "ESSRG"


# ── helpers ───────────────────────────────────────────────────────────────────

def load_data() -> tuple[list[dict], list[dict]]:
    """Load profiles and recipes."""
    with open(PROFILES_JSON) as f:
        profiles = json.load(f)

    with open(RECIPES_JSON) as f:
        recipes = json.load(f)

    return profiles, recipes


def prepare_neo4j_exports(profiles: list[dict], recipes: list[dict]):
    """
    Prepare Neo4j import CSVs:
    - neo4j_recipes_import.csv: Recipe nodes
    - neo4j_ingredients_import.csv: Ingredient nodes
    - neo4j_recipe_ingredient_rels.csv: Relationships
    """
    print("\n[1] Preparing Neo4j exports...")

    # Create a lookup: recipe_id → profile
    profile_lookup = {p["recipe_id"]: p for p in profiles}

    # Recipe nodes
    recipe_nodes = []
    ingredient_nodes = {}  # dedup
    relationships = []

    ingredient_counter = 0

    for recipe in recipes:
        recipe_id = recipe.get("recipe_id")
        profile = profile_lookup.get(recipe_id, {})

        # Add recipe node
        recipe_nodes.append({
            "recipe_id": recipe_id,
            "title": recipe.get("title", ""),
            "source": SOURCE,
            "serves": recipe.get("serves", 1.0),
            "duration_minutes": recipe.get("duration_minutes"),
            "tags": "|".join(recipe.get("tags", [])),
            "allergens": "|".join(recipe.get("allergens", [])),
            "energy_kcal": profile.get("energy_kcal", 0),
            "protein_g": profile.get("protein_g", 0),
            "fat_g": profile.get("fat_g", 0),
            "carbohydrate_g": profile.get("carbohydrate_g", 0),
            "coverage_percent": profile.get("coverage_percent", 0),
        })

        # Add ingredient nodes and relationships
        for ing in recipe.get("ingredients", []):
            ing_name = ing.get("name", "").strip()
            if not ing_name:
                continue

            # Deduplicated ingredient
            if ing_name not in ingredient_nodes:
                ingredient_counter += 1
                ingredient_nodes[ing_name] = {
                    "ingredient_id": f"ing_{ingredient_counter}",
                    "name": ing_name,
                }

            # Relationship: recipe → ingredient
            relationships.append({
                "recipe_id": recipe_id,
                "ingredient_id": ingredient_nodes[ing_name]["ingredient_id"],
                "quantity_g": ing.get("weight_g", 0),
                "measurement": ing.get("measurement", ""),
            })

    # Write CSVs
    recipes_df = pd.DataFrame(recipe_nodes)
    recipes_df.to_csv(NEO4J_RECIPES_CSV, index=False)
    print(f"    Wrote {len(recipes_df)} recipes to {NEO4J_RECIPES_CSV}")

    ingredients_df = pd.DataFrame(ingredient_nodes.values())
    ingredients_df.to_csv(NEO4J_INGREDIENTS_CSV, index=False)
    print(f"    Wrote {len(ingredients_df)} ingredients to {NEO4J_INGREDIENTS_CSV}")

    rels_df = pd.DataFrame(relationships)
    rels_df.to_csv(NEO4J_RELATIONSHIPS_CSV, index=False)
    print(f"    Wrote {len(rels_df)} relationships to {NEO4J_RELATIONSHIPS_CSV}")


def prepare_elasticsearch_export(recipes: list[dict], profiles: list[dict]):
    """
    Prepare Elasticsearch import CSV with searchable fields.
    """
    print("\n[2] Preparing Elasticsearch export...")

    profile_lookup = {p["recipe_id"]: p for p in profiles}
    es_docs = []

    for recipe in recipes:
        recipe_id = recipe.get("recipe_id")
        profile = profile_lookup.get(recipe_id, {})

        es_docs.append({
            "recipe_id": recipe_id,
            "title": recipe.get("title", ""),
            "source": SOURCE,
            "type": recipe.get("type"),
            "tags": " ".join(recipe.get("tags", [])),
            "allergens": " ".join(recipe.get("allergens", [])),
            "ingredients": " ".join(ing.get("name", "") for ing in recipe.get("ingredients", [])),
            "energy_kcal": profile.get("energy_kcal", 0),
            "serves": recipe.get("serves", 1.0),
            "duration_minutes": recipe.get("duration_minutes"),
        })

    es_df = pd.DataFrame(es_docs)
    es_df.to_csv(ELASTICSEARCH_CSV, index=False)
    print(f"    Wrote {len(es_df)} documents to {ELASTICSEARCH_CSV}")


def prepare_postgres_exports(recipes: list[dict], profiles: list[dict]):
    """
    Prepare PostgreSQL import CSVs:
    - postgres_recipes_import.csv: Recipe metadata
    - postgres_profiles_import.csv: Nutrition profiles
    """
    print("\n[3] Preparing PostgreSQL exports...")

    # Recipes table
    recipe_records = []
    for recipe in recipes:
        recipe_records.append({
            "recipe_id": recipe.get("recipe_id"),
            "source": SOURCE,
            "title": recipe.get("title", ""),
            "description": recipe.get("description"),
            "url": recipe.get("url"),
            "image_url": recipe.get("image_url"),
            "serves": recipe.get("serves", 1.0),
            "duration_minutes": recipe.get("duration_minutes"),
            "tags": "|".join(recipe.get("tags", [])),
            "allergens": "|".join(recipe.get("allergens", [])),
        })

    recipes_df = pd.DataFrame(recipe_records)
    recipes_df.to_csv(POSTGRES_RECIPES_CSV, index=False)
    print(f"    Wrote {len(recipes_df)} recipes to {POSTGRES_RECIPES_CSV}")

    # Profiles table
    profile_records = []
    for profile in profiles:
        profile_records.append({
            "recipe_id": profile.get("recipe_id"),
            "source": SOURCE,
            "region": "HU",  # Hungary
            "energy_kcal": profile.get("energy_kcal", 0),
            "energy_kcal_per_serving": profile.get("energy_kcal_per_serving", 0),
            "protein_g": profile.get("protein_g", 0),
            "protein_g_per_serving": profile.get("protein_g_per_serving", 0),
            "fat_g": profile.get("fat_g", 0),
            "fat_g_per_serving": profile.get("fat_g_per_serving", 0),
            "saturated_fat_g": profile.get("saturated_fat_g", 0),
            "saturated_fat_g_per_serving": profile.get("saturated_fat_g_per_serving", 0),
            "carbohydrate_g": profile.get("carbohydrate_g", 0),
            "carbohydrate_g_per_serving": profile.get("carbohydrate_g_per_serving", 0),
            "sugars_g": profile.get("sugars_g", 0),
            "sugars_g_per_serving": profile.get("sugars_g_per_serving", 0),
            "fiber_g": profile.get("fiber_g", 0),
            "fiber_g_per_serving": profile.get("fiber_g_per_serving", 0),
            "sodium_mg": profile.get("sodium_mg", 0),
            "sodium_mg_per_serving": profile.get("sodium_mg_per_serving", 0),
            "coverage_percent": profile.get("coverage_percent", 0),
        })

    profiles_df = pd.DataFrame(profile_records)
    profiles_df.to_csv(POSTGRES_PROFILES_CSV, index=False)
    print(f"    Wrote {len(profiles_df)} profiles to {POSTGRES_PROFILES_CSV}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("ESSRG Recipes: Database Load Preparation (Step 1.7)")
    print("=" * 70)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load data
    print("\n[0] Loading profiles and recipes...")
    profiles, recipes = load_data()
    print(f"    Loaded {len(profiles)} profiles")
    print(f"    Loaded {len(recipes)} recipes")

    # Prepare exports
    prepare_neo4j_exports(profiles, recipes)
    prepare_elasticsearch_export(recipes, profiles)
    prepare_postgres_exports(recipes, profiles)

    # Summary
    print("\n" + "=" * 70)
    print("LOAD PREPARATION SUMMARY")
    print("=" * 70)
    print(f"\nNeo4j imports:")
    print(f"  Recipes:       {NEO4J_RECIPES_CSV}")
    print(f"  Ingredients:   {NEO4J_INGREDIENTS_CSV}")
    print(f"  Relationships: {NEO4J_RELATIONSHIPS_CSV}")
    print(f"\nElasticsearch import:")
    print(f"  Documents:     {ELASTICSEARCH_CSV}")
    print(f"\nPostgreSQL imports:")
    print(f"  Recipes:       {POSTGRES_RECIPES_CSV}")
    print(f"  Profiles:      {POSTGRES_PROFILES_CSV}")

    print("\n" + "-" * 70)
    print("NEXT STEPS")
    print("-" * 70)
    print(f"""
To load into live databases:

Neo4j:
  LOAD CSV WITH HEADERS FROM 'file:///{NEO4J_RECIPES_CSV}' AS row
  CREATE (r:Recipe {{recipe_id: row.recipe_id, ...}})

Elasticsearch:
  Use the CSV in {ELASTICSEARCH_CSV} with bulk indexing API

PostgreSQL:
  \\COPY recipes FROM '{POSTGRES_RECIPES_CSV}' WITH CSV HEADER
  \\COPY profiles FROM '{POSTGRES_PROFILES_CSV}' WITH CSV HEADER
    """)

    print("\n[✓] Database load preparation complete")
    print("    CSVs ready for manual import or scripted load")


if __name__ == "__main__":
    main()
