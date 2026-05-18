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
        # Take the average if it's a range
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

def get_recipe1m_servings():
    # Load IDs from nutritional info file to narrow down search
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
    
    print(f"Streaming metadata to extract servings for {len(ids)} recipes...")
    with open(meta_path, "r") as f:
        for line in f:
            try:
                item = json.loads(line)
                if item["id"] in ids:
                    servings_map[item["id"]] = parse_servings(item.get("serves"))
            except:
                continue
    return servings_map

def analyze_dataset_per_serving():
    results = {}
    
    # 1. HealthyFoods
    print("Analyzing HealthyFoods (per serving)...")
    hf_path = Path("data/HealthyFoods/HealthyFood_recipes_nutrition.json")
    if hf_path.exists():
        with open(hf_path, "r") as f:
            hf_data = json.load(f)
        cals = [parse_calories(r.get("nutrition_per_serve", {}).get("Calories")) for r in hf_data["recipes"]]
        results["HealthyFoods"] = calculate_outliers(cals)
        
    # 2. MyPlate
    print("Analyzing MyPlate (per serving)...")
    mp_path = Path("data/MyPlate/myplate_recipes_nutrition_usda.json")
    if mp_path.exists():
        with open(mp_path, "r") as f:
            mp_data = json.load(f)
        cals = [parse_calories(r.get("totals_per_serving_usda", {}).get("energy_kcal")) for r in mp_data]
        results["MyPlate"] = calculate_outliers(cals)

    # 3. Irish SafeFood
    print("Analyzing Irish SafeFood (per serving)...")
    is_path = Path("data/mappings/Irish Recipes_SafeFood.xlsx")
    if is_path.exists():
        wb = openpyxl.load_workbook(is_path)
        ws = wb["in"]
        rows = list(ws.iter_rows(min_row=3, values_only=True))
        cals = [parse_calories(row[25]) for row in rows if len(row) > 25]
        results["Irish_SafeFood"] = calculate_outliers(cals)

    # 4. Recipe1M
    print("Analyzing Recipe1M (per serving)...")
    serv_map = get_recipe1m_servings()
    nutr_path = Path("data/processed/recipe1m/recipes_with_nutritional_info.json")
    if nutr_path.exists():
        with open(nutr_path, "r") as f:
            nutr_data = json.load(f)
        
        per_serving_cals = []
        for r in nutr_data:
            total_nrg = sum(parse_calories(ing.get("nrg")) or 0 for ing in r.get("nutr_per_ingredient", []))
            serves = serv_map.get(r["id"], 1.0) # Default to 1 if missing
            per_serving_cals.append(total_nrg / serves if serves > 0 else 0)
        
        results["Recipe1M"] = calculate_outliers(per_serving_cals)
        
    return results

def main():
    results = analyze_dataset_per_serving()
    
    report_md = "# Revised Nutritional Outlier Report (Calories per Serving)\n\n"
    report_md += "| Dataset | Total Recipes | Mean | Median | Upper Bound (IQR) | Outlier Count |\n"
    report_md += "|---|---|---|---|---|---|\n"
    
    for name, stats in results.items():
        if stats:
            report_md += f"| {name} | {stats['count']} | {stats['mean']:.2f} | {stats['median']:.2f} | **{stats['upper_bound']:.2f}** | {stats['outlier_count']} |\n"
            
    print("\n" + report_md)
    with open("revised_outlier_analysis.md", "w") as f:
        f.write(report_md)

if __name__ == "__main__":
    main()
