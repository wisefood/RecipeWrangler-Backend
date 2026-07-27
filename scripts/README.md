# Setup Scripts

These scripts populate the databases from raw data files. Run them once when setting up a new environment. They are **not** part of the running API.

## Prerequisites

- All four services running (see root README)
- `.env` configured with correct connection details
- `uv pip install -e .` completed from the repo root
- Install `uv pip install -e ".[dev-tools,experimental]"` as well if you need notebook/browser/Ollama extras

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

# FoodHero profiling trace (runs full profiling pipeline; skips recipes missing duration/serves)
# Dry-run example:
python scripts/postgres/import_foodhero_profile_trace.py --dry-run --limit 5 --region US
# Write to Postgres:
python scripts/postgres/import_foodhero_profile_trace.py --write --region US

# HealthyFoods profiling trace (runs full profiling pipeline with progress bar;
# skips recipes missing duration/serves and ignores notes)
# Dry-run example:
python scripts/postgres/import_healthyfoods_profile_trace.py --dry-run --limit 5 --region IE
# Write to Postgres:
python scripts/postgres/import_healthyfoods_profile_trace.py --write --region IE
```

### 2. Neo4j — graph enrichment

```bash
# Tag recipes with dietary labels (vegan, gluten-free, etc.)
python scripts/neo4j/tag_recipes.py

# Tag ingredients with allergen links
python scripts/neo4j/tag_allergens.py
```

### 3. Elasticsearch — vector collections

Import a validated embedding NDJSON file into a versioned physical index,
verify its count, and activate the stable alias:

```bash
python scripts/elasticsearch/import_vector_embeddings.py \
  --input exports/vector_embeddings.ndjson \
  --index ingredient_vectors_v1 \
  --alias ingredient_vectors \
  --recreate \
  --activate-alias
```

```dotenv
ELASTIC_VECTOR_INDEX=ingredient_vectors
ELASTIC_VECTOR_SEARCH_MODE=exact
```

`exact` is the migration-safe default for the current small, collection-filtered
indexes. `knn` enables approximate HNSW search; tune recall with
`ELASTIC_VECTOR_CANDIDATE_MULTIPLIER`.

### 4. Elasticsearch — recipe search index

```bash
# Rebuild the complete enriched recipe index from Neo4j + PostgreSQL
python scripts/elasticsearch/index_recipes_v2.py --recreate

# Refresh only selected Neo4j sources without dropping the index
python scripts/elasticsearch/index_recipes_v2.py --sources FoodHero HealthyFoods
```

## Notes

- All scripts read connection details from environment variables (`.env`).
- Postgres scripts use `NUTRITION_*` env vars; see `.env.example`.
- Vector scripts expect Elasticsearch at `ELASTIC_URL`.
- Vector exports are generated operational assets under `exports/` and are not
  committed to Git.
- `index_recipes_v2.py` builds the canonical recipe search index from Neo4j and
  enriches it with nutrition and sustainability scores from PostgreSQL.
- If Postgres data is lost (e.g. Docker volume wiped), re-run steps 1 and 2 from the dump your colleague shared, or re-run these scripts.
