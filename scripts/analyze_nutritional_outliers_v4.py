import json
import re
import numpy as np
from pathlib import Path
import openpyxl
from neo4j import GraphDatabase

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
    
    outliers = [v for v in vals if v > upper_bound]
    hard_outliers = [v for v in vals if v > 1000] # Hard threshold requested by user
    
    return {
        "count": len(vals),
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "q1": float(q1),
        "q3": float(q3),
        "iqr": float(iqr),
        "upper_bound": float(upper_bound),
        "outlier_count_iqr": len(outliers),
        "outlier_count_1000": len(hard_outliers),
        "outlier_percentage_1000": (len(hard_outliers) / len(vals)) * 100 if len(vals) > 0 else 0
    }

def get_recipe1m_servings():
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
    return servings_map

def main():
    results = {}
    serv_map = get_recipe1m_servings()
    
    # 1. HealthyFoods
    print("Analyzing HealthyFoods...")
    hf_path = Path("data/HealthyFoods/HealthyFood_recipes_nutrition.json")
    if hf_path.exists():
        with open(hf_path, "r") as f:
            hf_data = json.load(f)
        cals = [parse_calories(r.get("nutrition_per_serve", {}).get("Calories")) for r in hf_data["recipes"]]
        results["HealthyFoods"] = calculate_outliers(cals)
        
    # 2. MyPlate
    print("Analyzing MyPlate...")
    mp_path = Path("data/MyPlate/myplate_recipes_nutrition_usda.json")
    if mp_path.exists():
        with open(mp_path, "r") as f:
            mp_data = json.load(f)
        cals = [parse_calories(r.get("totals_per_serving_usda", {}).get("energy_kcal")) for r in mp_data]
        results["MyPlate"] = calculate_outliers(cals)

    # 3. Irish SafeFood
    print("Analyzing Irish SafeFood...")
    is_path = Path("data/mappings/Irish Recipes_SafeFood.xlsx")
    if is_path.exists():
        wb = openpyxl.load_workbook(is_path)
        ws = wb["in"]
        rows = list(ws.iter_rows(min_row=3, values_only=True))
        cals = [parse_calories(row[25]) for row in rows if len(row) > 25]
        results["Irish_SafeFood"] = calculate_outliers(cals)

    # 4. Recipe1M
    print("Analyzing Recipe1M...")
    nutr_path = Path("data/processed/recipe1m/recipes_with_nutritional_info.json")
    if nutr_path.exists():
        with open(nutr_path, "r") as f:
            nutr_data = json.load(f)
        
        per_serving_cals = []
        for r in nutr_data:
            total_nrg = sum(parse_calories(ing.get("nrg")) or 0 for ing in r.get("nutr_per_ingredient", []))
            serves = serv_map.get(r["id"], 4.0) # Use 4 as a reasonable default if missing
            per_serving_cals.append(total_nrg / serves)
        
        results["Recipe1M"] = calculate_outliers(per_serving_cals)

    # Generate Report
    report_md = "# Revised Nutritional Outlier Report (Per Serving)\n\n"
    report_md += "| Dataset | Total Recipes | Median Cal | Upper Bound (IQR) | Outliers (>IQR) | Outliers (>1000 kcal) |\n"
    report_md += "|---|---|---|---|---|---|\n"
    
    for name, stats in results.items():
        if stats:
            report_md += f"| {name} | {stats['count']} | {stats['median']:.2f} | {stats['upper_bound']:.2f} | {stats['outlier_count_iqr']} | **{stats['outlier_count_1000']}** |\n"
            
    print("\n" + report_md)
    with open("revised_outlier_report_per_serving.md", "w") as f:
        f.write(report_md)

if __name__ == "__main__":
    main()
