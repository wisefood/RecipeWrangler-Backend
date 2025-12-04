#%%
import pandas as pd
#%%
density_df = pd.read_excel("data/Density_DB_v2.xlsx", sheet_name='Density DB')
#%%
density_df
#%%
from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.chroma_client import get_chroma_client, CHROMA_HOST, CHROMA_PORT

# Add one embedding per row (names are unique)
density_df["embeddings"] = (
    density_df["Food name and description"]
    .astype(str)
    .apply(get_embeddings)
)

#%%
density_df
#%%
import pandas as pd

client = get_chroma_client()

collection = client.get_or_create_collection(
    name="foods_density_v1",
    metadata={"hnsw:space": "cosine"},
    embedding_function=None,
)

df = density_df.copy()

ids = df.index.astype(str).tolist()
documents = df["Food name and description"].astype(str).tolist()
embeddings = df["embeddings"].apply(lambda v: [float(x) for x in list(v)]).tolist()

# alias long -> short key; convert NaN -> None (JSON null)
alias = {
    "Food name and description": "Food name and description",
    "Density in g/ml (including mass and bulk density)": "Density in g/ml",
    "Specific gravity": "Specific gravity",
    "BiblioID": "BiblioID",
    "Update Version 2.0": "Update Version 2.0",
}

def to_meta(row):
    meta = {}
    for old, new in alias.items():
        val = row[old]
        # Chroma rejects None/NaN in metadata; drop missing fields entirely.
        if pd.isna(val):
            continue
        meta[new] = val
    return meta

metadatas = [to_meta(row) for _, row in df.iterrows()]

# upsert in batches
BATCH = 512
n = len(ids)
for i in range(0, n, BATCH):
    j = i + BATCH
    collection.upsert(
        ids=ids[i:j],
        embeddings=embeddings[i:j],
        documents=documents[i:j],
        metadatas=metadatas[i:j],
    )

print("Loaded rows:", collection.count())

#%%
client = get_chroma_client()

collections = client.list_collections()

print("Collections at", f"{CHROMA_HOST}:{CHROMA_PORT}")
for col in collections:
    print(f"- {col.name} (count={col.count()})")
