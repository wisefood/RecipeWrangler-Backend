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
# Irish ingredient nutrition
python scripts/postgres/import_irish_ingredients_nutrition_psql.py

# Hungarian ingredient nutrition (first export the normalized CSV)
python preprocessing/hungarian/export_hungarian_comp_table_csv.py
python scripts/postgres/import_hungarian_ingredients_nutrition_psql.py

# FoodHero profiling trace (runs full profiling pipeline; skips recipes missing duration/serves)
# Dry-run example:
python scripts/postgres/import_foodhero_profile_trace.py --dry-run --limit 5 --region IE
# Write to Postgres:
python scripts/postgres/import_foodhero_profile_trace.py --write --region IE

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

# Audit inferred FATO declarations (dry-run); add --apply only after reviewing
PYTHONPATH=src uv run python \
  scripts/neo4j/cleanup_allergen_declarations.py

# Build explicit vegan and vegetarian ingredient/recipe assessments
# (omit --apply for a read-only preview)
PYTHONPATH=src uv run python \
  scripts/neo4j/classify_vegan_vegetarian.py --apply
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
# Refresh every recipe through the protected catalog projection. Elasticsearch-
# owned annotations (course types, cuisines, moods, vibes, evidence) are kept.
uv run python scripts/maintenance/reproject_all_recipes.py --no-resume

# A versioned rebuild must use the catalog builder. Carry-over is enabled by
# default and is required to preserve colleague-provided annotations.
uv run python scripts/catalog/build_recipes.py \
  --new-index recipes_vNEXT --apply

# Add title-only semantic vectors after the final corpus is active. Re-running
# skips unchanged titles; use --replace only when changing the model.
uv run python scripts/catalog/embed_recipe_titles.py --apply
```

### 5. Cross-store data-quality audit

```bash
uv run python scripts/maintenance/audit_data_quality.py \
  --json-output artifacts/reports/data_quality.json \
  --markdown-output artifacts/reports/data_quality.md
```

The report covers active EU/Irish/Hungarian/Slovenian ingredient tables,
recipe-field completeness, the exact-four-profile invariant, and A–E/missing
Nutri-Score distributions overall and by recipe source.

## Notes

- All scripts read connection details from environment variables (`.env`).
- Postgres scripts use `NUTRITION_*` env vars; see `.env.example`.
- Vector scripts expect Elasticsearch at `ELASTIC_URL`.
- Vector exports are generated operational assets under `exports/` and are not
  committed to Git.
- `catalog/build_recipes.py` is the only full recipe-index builder.
- If Postgres data is lost (e.g. Docker volume wiped), re-run steps 1 and 2 from the dump your colleague shared, or re-run these scripts.
