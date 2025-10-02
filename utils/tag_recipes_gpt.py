import pandas as pd

pp_recipes_preprocessed = pd.read_pickle("data/pp_recipes_preprocessed_v3.pkl")

import sys, ast, pandas as pd
from tqdm.auto import trange

sys.path.insert(0, "/home/karvanitis/RecipeWrangler-Backend")
from tools.tagger_tool_gpt import Tagger_Node_gpt

def parse_list(x):
    """Parse a cell value into a list of items (always returns a list).

    - If it's already a list, return it.
    - If it's a string, try literal_eval; if that yields a non-list (e.g., a number), wrap it.
      Otherwise, fall back to comma-splitting.
    - If it's NaN/None, return []. Otherwise, wrap the single value.
    """
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = ast.literal_eval(x)
            if isinstance(v, list):
                return v
            return [v]
        except Exception:
            return [s.strip() for s in x.split(",") if s.strip()]
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return []
    return [x]

def to_minutes(x):
    if x is None or (isinstance(x, float) and pd.isna(x)): return 0
    try: return int(x)
    except: return 0

def safe_str(x):
    return x if isinstance(x, str) else ("" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x))

idx = pp_recipes_preprocessed.index[:1000]
for i in trange(len(idx), desc="Tagging 1000 recipes"):
    ridx = idx[i]
    row = pp_recipes_preprocessed.loc[ridx]
    out = Tagger_Node_gpt({
        "title": safe_str(row.get("title", "")),
        "ingredient_names": [safe_str(s) for s in parse_list(row.get("ingredient_names", []))],
        "directions": [safe_str(s) for s in parse_list(row.get("directions", []))],
        "total_time": to_minutes(row.get("duration", row.get("total_time", 0))),
        "tags": safe_str(row.get("tags", ""))  # avoid NaN causing .split error
    })
    pp_recipes_preprocessed.at[ridx, "tags"] = safe_str(out.get("tags", ""))
    pp_recipes_preprocessed.at[ridx, "allergens"] = safe_str(out.get("allergens", ""))

pp_recipes_preprocessed.to_pickle("pp_recipe_preprocessed_v3_tagged_gpt_1000.pkl")
