# Setup Scripts

These scripts populate the databases from raw data files. Run them once when setting up a new environment. They are **not** part of the running API.

## Prerequisites

- All four services running (see root README)
- `.env` configured with correct connection details
- `uv pip install -e .` completed from the repo root

## Order of execution

Run in this order when initialising a fresh environment:

### 1. PostgreSQL — ingredient nutrition tables

```bash
# USDA ingredient nutrition
python scripts/postgres/import_usda_ingredients_nutrition_psql.py

# Irish ingredient nutrition
python scripts/postgres/import_irish_ingredients_nutrition_psql.py

# Hungarian ingredient nutrition (first export the normalized CSV)
python preprocessing/hungarian/export_hungarian_comp_table_csv.py
python scripts/postgres/import_hungarian_ingredients_nutrition_psql.py

# USDA recipe-level nutrition totals
python scripts/postgres/import_usda_recipes_nutrition_psql.py

# USDA ingredient weights
python scripts/postgres/import_usda_weights.py

# MyPlate profiling trace (pre-computed profiles)
python scripts/postgres/import_myplate_profile_trace.py
```

### 2. Neo4j — graph enrichment

```bash
# Tag recipes with dietary labels (vegan, gluten-free, etc.)
python scripts/neo4j/tag_recipes.py

# Tag ingredients with allergen links
python scripts/neo4j/tag_allergens.py
```

### 3. Chroma — vector collections

```bash
# Build the Irish nutrition ingredient collection
python scripts/chroma/backfill_irish_chroma_metadata.py

# Build the Hungarian nutrition ingredient collection
python scripts/chroma/build_nutritional_chromadb_hungarian.py

# Build the USDA ingredient canonical collection
python scripts/chroma/rebuild_usda_ingredients_canonical_from_labels.py
```

### 4. Elasticsearch — search index

```bash
# Import recipe titles + metadata into the search index
python scripts/elasticsearch/import_recipes_to_elastic.py
```

## Notes

- All scripts read connection details from environment variables (`.env`).
- Postgres scripts use `NUTRITION_*` env vars; see `.env.example`.
- Chroma scripts expect a running Chroma server at `CHROMA_HOST:CHROMA_PORT`.
- The Elasticsearch script expects recipes exported from Neo4j as a JSON file — check the script's `--input` argument.
- If Postgres data is lost (e.g. Docker volume wiped), re-run steps 1 and 2 from the dump your colleague shared, or re-run these scripts.
