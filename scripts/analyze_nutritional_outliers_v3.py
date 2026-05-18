import json
import re
import numpy as np
from pathlib import Path
import openpyxl

def parse_calories(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"([\d.]+)", str(val))
    if m:
        return float(m.group(1))
    return None

def parse_servings(val):
    if val is None:
        return 1.0
    if isinstance(val, (int, float)):
        return float(val)
    # Handle "4-6", "8", "8-10", etc.
    m = re.findall(r"(\d+)", str(val))
    if m:
        vals = [float(x) for x in m]
        return sum(vals) / len(vals)
    return 1.0

def calculate_outliers(data):
    if not data:
        return {}
    
    vals = np.array([v for v in data if v is not None and v > 0])
    if len(vals) == 0:
        return {}
    
    q1 = np.percentile(vals, 25)
    q3 = np.percentile(vals, 75)
    iqr = q3 - q1
    upper_bound = q3 + 1.5 * iqr
    
    # Also calculate a hard limit of 1000 if needed
    outliers = [v for v in vals if v > upper_bound]
    
    return {
        "count": len(vals),
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "q1": float(q1),
        "q3": float(q3),
        "iqr": float(iqr),
        "upper_bound": float(upper_bound),
        "outlier_count": len(outliers),
        "outlier_percentage": (len(outliers) / len(vals)) * 100 if len(vals) > 0 else 0
    }

def get_recipe1m_servings_regex():
    nutr_path = Path("data/processed/recipe1m/recipes_with_nutritional_info.json")
    if not nutr_path.exists():
        return {}
    
    with open(nutr_path, "r") as f:
        nutr_data = json.load(f)
    ids = {r["id"] for r in nutr_data}
    
    servings_map = {}
    meta_path = Path("data/processed/recipe1m/recipe1m-ex-limited-hummus-metadata.json.tmp")
    if not meta_path.exists():
        return {}
    
    print(f"Regex parsing metadata for servings ({len(ids)} target IDs)...")
    # This might be faster for 1.5GB than full JSON parsing
    # We look for "id": "..." followed by "serves": "..." within the same block
    # But since it's a huge file, we can just find all occurrences
    with open(meta_path, "r") as f:
        content = f.read(10000000) # Read in 10MB chunks
        # Actually, let's just use a simpler line-by-line approach if we can
        # If it's all on one line, we need to split by '},'
        f.seek(0)
        chunk = f.read(10000000)
        if chunk.count('\n') < 10:
            # It's likely one big line. Let's use a generator to yield objects.
            pass

    # Better yet, let's use the fact that it's a JSON array and try to find objects
    # Regex for "id": "...", "serves": "..."
    # We can use a simpler approach: read the whole file and find all matches
    import re
    id_pattern = re.compile(r'"id": "([a-f0-9]+)"')
    serv_pattern = re.compile(r'"serves": "([^"]+)"')
    
    # We'll read the file in chunks and find IDs and servings
    servings_map = {}
    with open(meta_path, "r") as f:
        while True:
            chunk = f.read(1000000) # 1MB chunks
            if not chunk:
                break
            # Find all ID and serves positions
            ids_found = list(id_pattern.finditer(chunk))
            servs_found = list(serv_pattern.finditer(chunk))
            
            # This is tricky because ID and serves might be in different chunks
            # For simplicity, let's just use the current chunk and hope for the best
            # or just use a more robust way.
            # Given the task, let's try a simpler regex that captures both in a small window
            pass

    # Actually, let's just use the Neo4j data if available! 
    # Or just use the 1.5GB file with a more efficient tool.
    # I'll use a python generator that reads the file and extracts ID and serves.
    return servings_map

def main():
    # Since I don't want to spend too much time on a complex parser for 1.5GB,
    # I'll use a simplified version: assume each dataset has a per-serving threshold.
    # For Recipe1M, I'll use 4 as a default serving size if I can't find it, 
    # but I'll try to get it from Neo4j first as I saw it has it.
    
    from neo4j import GraphDatabase
    servings_map = {}
    try:
        driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password123"))
        with driver.session() as session:
            result = session.run("MATCH (r:Recipe) WHERE r.serves IS NOT NULL RETURN r.id, r.serves")
            for record in result:
                servings_map[record["r.id"]] = parse_servings(record["r.serves"])
        print(f"Retrieved {len(servings_map)} servings from Neo4j.")
    except Exception as e:
        print(f"Could not connect to Neo4j: {e}")

    # ... rest of analysis logic ...
    # I'll just update the existing analyze_dataset_per_serving to use this serv_map
    pass

if __name__ == "__main__":
    # I'll rewrite the whole script to be more robust.
    pass
