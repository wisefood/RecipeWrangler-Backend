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
    # Handle strings like "298 cal", "1200 kcal", etc.
    m = re.search(r"([\d.]+)", str(val))
    if m:
        return float(m.group(1))
    return None

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
    lower_bound = max(0, q1 - 1.5 * iqr)
    
    outliers = [v for v in vals if v > upper_bound or v < lower_bound]
    
    return {
        "count": len(vals),
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "q1": float(q1),
        "q3": float(q3),
        "iqr": float(iqr),
        "upper_bound": float(upper_bound),
        "lower_bound": float(lower_bound),
        "outlier_count": len(outliers),
        "outlier_percentage": (len(outliers) / len(vals)) * 100 if len(vals) > 0 else 0
    }

def analyze_healthy_foods():
    path = Path("data/HealthyFoods/HealthyFood_recipes_nutrition.json")
    if not path.exists():
        return None
    
    with open(path, "r") as f:
        data = json.load(f)
    
    calories = []
    for r in data.get("recipes", []):
        c = r.get("nutrition_per_serve", {}).get("Calories")
        calories.append(parse_calories(c))
    
    return calculate_outliers(calories)

def analyze_recipe1m():
    path = Path("data/processed/recipe1m/recipes_with_nutritional_info.json")
    if not path.exists():
        return None
    
    with open(path, "r") as f:
        data = json.load(f)
    
    cals_1m = []
    for r in data:
        total_nrg = sum(parse_calories(ing.get("nrg")) or 0 for ing in r.get("nutr_per_ingredient", []))
        cals_1m.append((r["title"], total_nrg))
    
    raw_cals = [c for t, c in cals_1m if c > 0]
    stats_1m = calculate_outliers(raw_cals)
    
    extreme = []
    if stats_1m:
        ub = stats_1m["upper_bound"]
        extreme = sorted([(t, c) for t, c in cals_1m if c > ub], key=lambda x: x[1], reverse=True)[:10]
            
    return stats_1m, extreme

def analyze_irish_safefood():
    path = Path("data/mappings/Irish Recipes_SafeFood.xlsx")
    if not path.exists():
        return None
    
    wb = openpyxl.load_workbook(path)
    ws = wb["in"]
    rows = list(ws.iter_rows(min_row=3, values_only=True))
    
    calories = []
    for row in rows:
        if len(row) > 25:
            val = row[25]
            calories.append(parse_calories(val))
            
    return calculate_outliers(calories)

def analyze_slovenian_opkp():
    path = Path("data/Slovenian_OPKP/primeri_receptov_all_info_per_recipe.json")
    if not path.exists():
        return None
    
    with open(path, "r") as f:
        data = json.load(f)
    
    calories = []
    for r_id, r_data in data.get("recipes", {}).items():
        for nv in r_data.get("nutritional_values", []):
            if nv.get("EuroFIR component code [ecompid] (components thesauri)") == "ENERC":
                val = nv.get("selected value [selval]")
                unit = nv.get("unit  (units thesauri) [unit]")
                if unit == "kcal":
                    calories.append(parse_calories(val))
                elif unit == "kJ":
                    calories.append(parse_calories(val) / 4.184)
                break
    
    return calculate_outliers(calories)

def analyze_myplate():
    path = Path("data/MyPlate/myplate_recipes_nutrition_usda.json")
    if not path.exists():
        return None
    
    with open(path, "r") as f:
        data = json.load(f)
    
    calories = []
    for r in data:
        c = r.get("totals_per_serving_usda", {}).get("energy_kcal")
        calories.append(parse_calories(c))
    
    return calculate_outliers(calories)

def main():
    results = {}
    sample_outliers = {}
    
    print("Analyzing HealthyFoods...")
    results["HealthyFoods"] = analyze_healthy_foods()
    
    print("Analyzing Recipe1M...")
    stats_1m, extreme_1m = analyze_recipe1m()
    results["Recipe1M"] = stats_1m
    sample_outliers["Recipe1M"] = extreme_1m

    print("Analyzing Irish_SafeFood...")
    results["Irish_SafeFood"] = analyze_irish_safefood()
    
    print("Analyzing Slovenian_OPKP...")
    results["Slovenian_OPKP"] = analyze_slovenian_opkp()
    
    print("Analyzing MyPlate...")
    results["MyPlate"] = analyze_myplate()
    
    # Generate Markdown Report
    report_md = "# Nutritional Outlier Report\n\n"
    report_md += "| Dataset | Total Recipes | Mean Cal | Median Cal | Upper Bound (Outlier Threshold) | Outlier Count | Outlier % |\n"
    report_md += "|---" * 7 + "|\n"
    
    for name, stats in results.items():
        if stats:
            report_md += f"| {name} | {stats['count']} | {stats['mean']:.2f} | {stats['median']:.2f} | **{stats['upper_bound']:.2f}** | {stats['outlier_count']} | {stats['outlier_percentage']:.2f}% |\n"
        else:
            report_md += f"| {name} | N/A | N/A | N/A | N/A | N/A | N/A |\n"
            
    if "Recipe1M" in sample_outliers and sample_outliers["Recipe1M"]:
        report_md += "\n## Sample Outliers (Recipe1M)\n\n"
        report_md += "| Title | Total Calories |\n|---|---|\n"
        for title, cal in sample_outliers["Recipe1M"]:
            report_md += f"| {title} | {cal:.2f} |\n"

    print("\n" + report_md)
    
    with open("nutritional_outlier_report.md", "w") as f:
        f.write(report_md)

if __name__ == "__main__":
    main()
