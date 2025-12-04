import ast
from tqdm.auto import trange
from recipe_wrangler.tools.ingredient_weight_tool import (
    ingredient_weight_tool_open as weight_tool,
)

import pandas as pd

df = pd.read_pickle("data/pp_recipes_preprocessed_v3.pkl")

# Take first 1000 rows
pp_recipes_preprocessed_weighted_1000 = df.head(1000).copy()

def as_list(x):
    if isinstance(x, list): return x
    if isinstance(x, str):
        try: return ast.literal_eval(x)
        except: return [s.strip() for s in x.split(",") if s.strip()]
    return [] if x is None else [x]

weights = []
for i in trange(len(pp_recipes_preprocessed_weighted_1000), desc="Weights (first 1000)"):
    row = pp_recipes_preprocessed_weighted_1000.iloc[i]
    names = as_list(row["ingredient_names"])
    measures = as_list(row["measurements"])
    res = weight_tool.invoke({"ingredient_names": names, "measurements": measures})
    weights.append(res if isinstance(res, list) else [])

pp_recipes_preprocessed_weighted_1000["weights"] = weights

# Optional: save to CSV
pp_recipes_preprocessed_weighted_1000.to_pickle("data/pp_recipes_preprocessed_v3_weighted_1000.pkl")
