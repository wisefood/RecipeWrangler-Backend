import math
import pandas as pd
from tqdm.auto import tqdm  # auto picks notebook/terminal renderer

from recipe_wrangler.tools.recipe_profiling_tool import Recipe_Profiling_Tool

INPUT_PKL = "data/pp_recipes_preprocessed_v3_weighted_and_tagged_1000.pkl"
OUTPUT_PKL = "data/pp_recipes_preprocessed_v3_weighted_and_tagged_1000_profiled.pkl"

TARGET_COLS = [
    "total_protein_g_per_serving_irish",
    "total_carbohydrate_g_per_serving_irish",
    "total_fat_g_per_serving_irish",
    "total_energy_kcal_per_serving_irish",
    "total_sugar_g_per_serving_irish",
    "total_saturated_fat_g_per_serving_irish",
    "total_sodium_mg_per_serving_irish",
    "total_sustainability",
    "total_sustainability_per_serving",
]
ROOT_LEVEL_COLS = ["sustainability_per_kg"]


def safe_list(value):
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return list(value) if isinstance(value, tuple) else [value]


def safe_float(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


df = pd.read_pickle(INPUT_PKL).copy()

for col in TARGET_COLS + ROOT_LEVEL_COLS:
    if col not in df.columns:
        df[col] = pd.NA

for idx, row in tqdm(df.iterrows(), total=len(df), desc="Profiling recipes"):
    names = [str(x) for x in safe_list(row.get("ingredient_names"))]
    measures = [str(x) for x in safe_list(row.get("measurements"))]
    weights = [w for w in (safe_float(x) for x in safe_list(row.get("weights"))) if w is not None]

    limit = min(len(names), len(measures), len(weights))
    if limit == 0:
        continue

    payload = {
        "title": row.get("title", f"Recipe #{idx}"),
        "ingredient_names": names[:limit],
        "measurements": measures[:limit],
        "weights": weights[:limit],
        "serving_size_g": safe_float(row.get("servingSize [g]")),
        "serves": safe_float(row.get("servingsPerRecipe")),
        "min_similarity": 0.5,
    }

    profile = Recipe_Profiling_Tool(payload)
    totals = profile.get("totals", {})

    for col in TARGET_COLS:
        df.at[idx, col] = totals.get(col)
    for col in ROOT_LEVEL_COLS:
        df.at[idx, col] = profile.get(col)

df.to_pickle(OUTPUT_PKL)
print(f"Wrote profiled recipes to {OUTPUT_PKL}")
