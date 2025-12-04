from pathlib import Path

import pandas as pd 

REPO_ROOT = Path(__file__).resolve().parents[3]

df= pd.read_csv('data/pp_recipes_preprocessed_v2.csv') 
df = df.iloc[:1000]

def unique_ingredients(df, col='ingredient_names'):
    s = df[col]

    def to_list(v):
        if isinstance(v, list):
            return v
        if pd.isna(v):
            return []
        return [p.strip() for p in str(v).split(',')]

    def to_name(x):
        if isinstance(x, dict) and 'name' in x:
            return x['name']
        return x

    ser = (s.map(to_list)
             .explode()
             .map(to_name)
             .astype(str)
             .str.strip()
             .str.lower())

    ser = ser[(ser != '') & (ser != 'nan')]
    return sorted(set(ser))

unique_ings = unique_ingredients(df)

from tqdm.auto import tqdm
from recipe_wrangler.tools.ingredient_embeddings_tool import (
    ensure_ingredients_in_collection,
)

unique_ings = unique_ings  # your list of names
BATCH = 128  # matches the tool's internal batch size

found_all = set()
created_all = []
failed_all = []

state = {
    "persist_path": str(REPO_ROOT / "chroma_db"),
    "collection_name": "ingredients",
    "debug": False,  # set True if you also want internal logs
}

for start in tqdm(range(0, len(unique_ings), BATCH),
                  desc="Embedding + upserting",
                  unit="batch"):
    batch = unique_ings[start:start+BATCH]
    out = ensure_ingredients_in_collection.func(batch, state)
    found_all.update(out["found"])
    created_all.extend(out["created"])
    failed_all.extend(out["failed"])

print(f"Done. Found={len(found_all)} Created={len(created_all)} Failed={len(failed_all)}")
if failed_all:
    print("Failed examples:", failed_all[:10])
