import json
import requests
from pathlib import Path
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

# Database Configs
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "password123"
ELASTIC_URL = "http://localhost:9200"
ELASTIC_INDEX = "recipes"
POSTGRES_URL = "postgresql://rag:rag@localhost:5432/rag"

BATCH_SIZE = 500

def get_removed_ids_by_source():
    sources = {
        "HealthyFoods": {"orig": Path("data/HealthyFoods/HealthyFood_recipes_nutrition.json"), 
                         "clean": Path("data/HealthyFoods/HealthyFood_recipes_nutrition_clean.json"), 
                         "id_field": "url", "nested": "recipes"},
        "MyPlate": {"orig": Path("data/MyPlate/myplate_recipes_nutrition_usda.json"), 
                    "clean": Path("data/MyPlate/myplate_recipes_nutrition_usda_clean.json"), 
                    "id_field": "recipe_id"},
        "Recipe1M": {"orig": Path("data/processed/recipe1m/recipes_with_nutritional_info.json"), 
                     "clean": Path("data/processed/recipe1m/recipes_with_nutritional_info_clean.json"), 
                     "id_field": "id"}
    }
    
    removed = {}
    for name, cfg in sources.items():
        if not cfg["orig"].exists() or not cfg["clean"].exists(): continue
        with open(cfg["orig"], "r") as f: orig_data = json.load(f)
        with open(cfg["clean"], "r") as f: clean_data = json.load(f)
        
        orig_list = orig_data[cfg["nested"]] if cfg.get("nested") else orig_data
        clean_list = clean_data[cfg["nested"]] if cfg.get("nested") else clean_data
        
        orig_ids = {str(r[cfg["id_field"]]) for r in orig_list if cfg["id_field"] in r}
        clean_ids = {str(r[cfg["id_field"]]) for r in clean_list if id_field in r} if False else set()
        # Wait, I already have the clean files, so I can just subtract
        clean_ids = {str(r[cfg["id_field"]]) for r in clean_list if cfg["id_field"] in r}
        removed[name] = list(orig_ids - clean_ids)
        
    return removed

def sync_deletions():
    removed_by_source = get_removed_ids_by_source()
    all_ids = []
    for ids in removed_by_source.values(): all_ids.extend(ids)
    
    if not all_ids:
        print("No IDs to synchronize.")
        return

    print(f"Synchronizing {len(all_ids)} deletions across databases...")

    # 1. Elasticsearch
    print("Deleting from Elasticsearch...")
    # Use id for Recipe1M/MyPlate and url for HealthyFoods
    # We can just use a terms query on multiple fields
    payload = {
        "query": {
            "bool": {
                "should": [
                    {"terms": {"id": all_ids}},
                    {"terms": {"recipe_id": all_ids}},
                    {"terms": {"url": all_ids}}
                ]
            }
        }
    }
    try:
        url = f"{ELASTIC_URL}/{ELASTIC_INDEX}/_delete_by_query"
        res = requests.post(url, json=payload, timeout=600).json()
        print(f"  Elasticsearch: Deleted {res.get('deleted', 0)} records.")
    except Exception as e:
        print(f"  Elasticsearch failed: {e}")

    # 2. Postgres
    print("Deleting from Postgres...")
    engine = create_engine(POSTGRES_URL)
    with engine.connect() as conn:
        for i in range(0, len(all_ids), BATCH_SIZE):
            batch = all_ids[i:i+BATCH_SIZE]
            conn.execute(text('DELETE FROM "nutrients-recipe-profiles" WHERE recipe_id = ANY(:ids)'), {"ids": batch})
            conn.commit()
        print("  Postgres: Deletion complete.")

    # 3. Neo4j
    print("Deleting from Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with driver.session() as session:
        # Create indexes first to ensure speed
        session.run("CREATE INDEX recipe_url_idx IF NOT EXISTS FOR (r:Recipe) ON (r.url)")
        session.run("CREATE INDEX recipe_id_idx IF NOT EXISTS FOR (r:Recipe) ON (r.id)")
        
        for source, ids in removed_by_source.items():
            if not ids: continue
            print(f"  Neo4j: Processing {source} ({len(ids)} recipes)...")
            
            # Choose best field for source
            field = "url" if source == "HealthyFoods" else "recipe_id"
            
            query = f"""
            UNWIND $ids AS rid
            MATCH (r:Recipe)
            WHERE r.{field} = rid
            DETACH DELETE r
            """
            
            for i in range(0, len(ids), BATCH_SIZE):
                batch = ids[i:i+BATCH_SIZE]
                session.run(query, {"ids": batch})
                
        print("  Neo4j: Deletion complete.")

if __name__ == "__main__":
    sync_deletions()
