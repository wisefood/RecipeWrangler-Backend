# RecipeWrangler Backend

FastAPI backend for recipe search, parsing, nutrition profiling, and sustainability scoring. Built with LangGraph pipelines, a Neo4j recipe graph, and multi-source nutrition data.

---

## Architecture Overview

```
Client
  │
  ▼
FastAPI (src/recipe_wrangler/api/)
  │
  ├── GET   /health
  ├── GET   /api/v1/recipes/autocomplete                  → Elasticsearch
  ├── GET   /api/v1/recipes/{recipe_id}                   → Neo4j + PostgreSQL
  ├── POST  /api/v1/recipes/search                        → Neo4j + Groq LLM + Elasticsearch (fallback)
  ├── POST  /api/v1/recipes/param_search                  → Neo4j
  ├── POST  /api/v1/recipes/profile                       → Groq LLM + Chroma + PostgreSQL
  ├── POST  /api/v1/recipes/create                        → Neo4j + PostgreSQL (profiling pipeline)
  ├── POST  /api/v1/recipes/{recipe_id}/substitute        → Neo4j + Chroma + PostgreSQL
  └── PATCH /api/v1/recipes/{recipe_id}                   → Neo4j + Elasticsearch

Services
  ├── Neo4j         – Recipe knowledge graph (recipes, ingredients, allergens, diet tags)
  ├── Elasticsearch – Recipe title search index
  ├── Chroma        – Vector store for nutrition and sustainability matching
  ├── PostgreSQL    – Structured nutrition data (USDA + Irish) and profiling traces
  └── Groq LLM      – Recipe parsing, natural language → Cypher, weight estimation
```

---

## Endpoints

### GET /health
Simple health check. Returns `{"status": "ok"}`.

---

### GET /api/v1/recipes/autocomplete

**Purpose:** Real-time recipe title suggestions for a search box.

**Parameters:** `q` (2–120 chars), `limit` (1–20, default 8)

**Flow:**
```
q="chick"
  └── Elasticsearch bool_prefix query on title field
        └── Deduplicate (case-fold)
              └── Return top N suggestions
```

**Databases:** Elasticsearch only.

**Example:**
```bash
curl "http://localhost:8001/api/v1/recipes/autocomplete?q=chick&limit=5"
```

---

### GET /api/v1/recipes/{recipe_id}

**Purpose:** Fetch full recipe details including ingredients, instructions, and nutrition breakdown.

**Flow:**
```
recipe_id
  ├── Neo4j      → title, ingredients, instructions, tags, duration, serves
  └── PostgreSQL → stored nutrition totals + profiling trace
        └── Compute Nutri-Score breakdown
              └── Return RecipeDetailResponse
```

**Databases:** Neo4j, PostgreSQL.

**Returns:** Title, ingredients with quantities, step-by-step instructions, diet tags, Nutri-Score (A–E), macro/micronutrients per serving, carbon footprint.

When Postgres has source-provided nutrition for the recipe, the response also includes:

- `has_ground_truth_nutrition`
- `ground_truth_nutrition_source`
- `ground_truth_nutrition.nutrients_per_serving`

Those fields expose original/source per-serving nutrition separately from computed regional profiles. SafeFood rows use `nutrition_source="safefood"`, Recipe1M original nutrition uses `nutrition_source="recipe1m_original"`, and HealthyFoods source nutrition uses `nutrition_source="healthyfoods_original"` when available. Computed profiles such as `usda`, `irish`, and `hungarian` are not marked as ground truth.

---

### POST /api/v1/recipes/search

**Purpose:** Natural language recipe search. Translates a question into a graph query.

**Input:**
```json
{
  "question": "chicken and rice under 30 minutes",
  "exclude_allergens": ["peanut"]
}
```

**Flow:**
```
question
  │
  ├── Empty question? → random MyPlate recipes from Elasticsearch
  │
  └── LangGraph pipeline (text2cypher_v2):
        1. Extract_Constraints (Groq LLM)
           → {preferred_ingredients, excluded_ingredients, allergens,
              diet_tags, title_keywords, max_duration, limit}
        2. Map_Constraints_to_Cypher
           → parameterised Cypher (no freeform generation)
        3. Execute on Neo4j
        4. Fallback: param_search → Elasticsearch random (if pipeline fails)
  │
  └── Normalise results
        ├── Attach Nutri-Score colours (PostgreSQL)
        ├── Attach recipe scores
        └── Return recipe cards
```

**Databases:** Neo4j (primary), Elasticsearch (fallback).
**LLM:** Groq (constraint extraction only – Cypher is template-based, not free-form).

---

### POST /api/v1/recipes/param_search

**Purpose:** Deterministic, parameter-driven recipe filtering. No LLM involved.

**Input:**
```json
{
  "include_ingredients": ["chicken", "rice"],
  "exclude_ingredients": ["cream"],
  "exclude_allergens": ["gluten"],
  "diet_tags": ["dairy-free"],
  "dish_types": ["breakfast"],
  "max_duration_minutes": 45,
  "limit": 10,
  "offset": 0
}
```

**Flow:**
```
filters
  └── Build parameterised Cypher
        ├── ALL(ingredient IN include_ingredients WHERE EXISTS in graph)
        ├── NOT EXISTS excluded_ingredients
        ├── NOT EXISTS allergens via HAS_ALLERGEN
        ├── EXISTS diet_tags via HAS_TAG
        ├── EXISTS dish_types via HAS_TAG to Tag category=dish-type
        └── duration <= max_duration_minutes
  └── Execute on Neo4j → enriched recipe cards
        └── Attach image_url, duration, serves, nutri_score from PostgreSQL
```

Results are ordered deterministically for pagination: expert recipes first, then preferred catalog sources (`FoodHero` and `HealthyFoods`), then `has_profile=true` recipes, then recipes with duration and serving metadata, then stable recipe identity fields. Empty filter payloads return a stable profiled-recipe catalog page using `limit` and `offset`.

**Databases:** Neo4j, PostgreSQL.

---

### POST /api/v1/recipes/profile

**Purpose:** Parse raw recipe text and return full nutrition + sustainability profile.

**Input:**
```json
{
  "raw_recipe": "Chicken Tikka Masala\n2 chicken breasts...",
  "region": "IE",
  "persist_trace": false
}
```

**Flow:**
```
raw_recipe
  │
  └── LangGraph pipeline (recipe_profiling_chain):
        │
        ├── 1. Parse (Groq LLM)
        │      → title, ingredients[], measurements[], directions[], serves, total_time
        │
        ├── 2. Weigh (USDA weight tool)
        │      → grams per ingredient
        │      Fallback chain (in order):
        │        a) USDA canonical name match
        │        b) Chroma embedding match → Postgres lookup
        │        c) Recipe1M LLM weight CSV
        │        d) FDA/LLM unit-grams CSV
        │        e) Live Groq LLM (last resort)
        │        f) Herb/spice default (0.3g per pinch)
        │
        ├── 3. Nutrition (Chroma + PostgreSQL)
        │      → Query Chroma for best ingredient match
        │      → Fetch per-100g values from Postgres
        │      → Scale by weight, sum totals + per-serving
        │      → Source: Irish table (region=IE) or USDA (region=US)
        │
        ├── 4. Sustainability (Chroma)
        │      → Match ingredient to carbon footprint DB
        │      → kg CO2e per ingredient → total + per-serving
        │
        └── 5. Nutri-Score (A–E)
               → Computed from macros (energy, saturated fat,
                 sugar, sodium, protein, fibre, fruit/veg)
               → Optional: persist trace to PostgreSQL
```

**Databases:** Chroma (nutrition + sustainability matching), PostgreSQL (nutrient data + trace storage).
**LLM:** Groq (parsing + weight fallback only).

---

### POST /api/v1/recipes/

**Purpose:** Create a new recipe and immediately compute its nutrition profile and Nutri-Score.

**Input:**
```json
{
  "title": "Pasta Carbonara",
  "ingredients": ["100g spaghetti", "2 eggs", "50g bacon", "30g parmesan"],
  "instructions": ["Cook pasta", "Fry bacon", "Mix and serve"],
  "serves": 2,
  "duration": 20,
  "region": "IE",
  "image_url": null,
  "tags": ["gluten-free"],
  "allergens": ["egg", "milk"]
}
```

`region` must be `IE`, `US`, or `HU`. `instructions`, `image_url`, `tags`, `allergens` are optional.

**Flow:**
```
ingredients + title
  └── Profiling pipeline (same as /profile)
        └── Nutri-Score computed
              └── Write recipe node to Neo4j
                    └── Write nutrition trace to PostgreSQL
                          └── Return {recipe_id, message}
```

**Databases:** Neo4j (recipe node), PostgreSQL (nutrition trace).
**LLM:** Groq (weight estimation fallback).

**Example:**
```bash
curl -X POST http://localhost:8001/api/v1/recipes/ \
  -H "Content-Type: application/json" \
  -d '{"title":"Omelette","ingredients":["2 eggs","10g butter"],"serves":1,"duration":5}'
```

---

### POST /api/v1/recipes/{recipe_id}/substitute

**Purpose:** Find the best substitute for a given ingredient in a recipe and return the full nutrition profile of the modified recipe.

**Input:**
```json
{
  "ingredient": "butter",
  "region": "IE"
}
```

`region` must be `IE`, `US`, or `HU`. Defaults to `IE`.

**Flow:**
```
recipe_id + ingredient
  │
  ├── Neo4j → fetch recipe (title, ingredients, measurements, serves)
  │
  ├── Substitution lookup (in order):
  │     1. HAS_SUBSTITUTION edges (MISKG-curated) — exact name match
  │     2. HAS_SUBSTITUTION edges (MISKG-curated) — single-word-qualifier variants
  │          e.g. "salted butter", "clarified butter" for query "butter"
  │     3. FoodOn taxonomy — progressive depth (1 hop, then 2, then 3)
  │          stops at the tightest ancestor that yields results
  │
  ├── 404 if no substitutes found
  │
  ├── Swap ingredient name in recipe (keep original measurement)
  │
  └── Re-profile with Recipe_Profiling_Chain_Structured (no LLM parse)
        → Chroma + PostgreSQL nutrition lookup
        → Nutri-Score recomputed
        → Return original_ingredient, substitute, candidates, modified nutrition profile
```

**Databases:** Neo4j (recipe + substitution graph), Chroma (nutrition matching), PostgreSQL (nutrient data).

**Returns:** `original_ingredient`, `substitute` (best pick), `substitution_source` (`graph_direct` or `foodon_taxonomy`), `candidates` (all found), `modified_recipe_profile` (full profiling output).

**Example:**
```bash
curl -X POST http://localhost:8001/api/v1/recipes/dfe70383db/substitute \
  -H "Content-Type: application/json" \
  -d '{"ingredient": "butter", "region": "IE"}'
```

---

### PATCH /api/v1/recipes/{recipe_id}

**Purpose:** Update mutable fields on an existing recipe.

**Input:**
```json
{
  "instructions": ["Updated step 1", "Updated step 2"],
  "image_url": "https://example.com/image.jpg"
}
```

Both fields are optional. At least one must be provided.

**Flow:**
```
recipe_id + fields
  ├── Neo4j  → update instructions / image_url on Recipe node
  └── Elasticsearch → reindex image_url if changed
        └── Return {recipe_id, updated_fields, message}
```

Returns 404 if the recipe does not exist in Neo4j.

**Databases:** Neo4j, Elasticsearch.

**Example:**
```bash
curl -X PATCH http://localhost:8001/api/v1/recipes/3758007968 \
  -H "Content-Type: application/json" \
  -d '{"instructions": ["Step 1", "Step 2", "Serve hot"]}'
```

---

## Services & Data Stores

| Service | Role | Docker |
|---|---|---|
| **Neo4j** | Recipe graph: recipes, ingredients, allergens, diet tags | `neo4j-docker/docker-compose.yml` |
| **Elasticsearch** | Recipe title autocomplete and fallback search | `elasticsearch-docker/docker-compose.yml` |
| **Chroma** | Vector similarity for nutrition & sustainability matching | `chromadb-docker/docker-compose.yml` |
| **PostgreSQL** | Nutrition data (USDA + Irish) and profiling traces | `postgresql-docker/docker-compose.yml` |

---

## Running Locally

### 1. Start services

```bash
docker compose -f neo4j-docker/docker-compose.yml up -d
docker compose -f postgresql-docker/docker-compose.yml up -d
docker compose -f chromadb-docker/docker-compose.yml up -d
docker compose -f elasticsearch-docker/docker-compose.yml up -d
```

### 2. Install dependencies

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 3. Configure environment

```bash
cp .env.example .env  # edit with your values
```

Required:
```
NEO4J_URI=bolt://localhost:7687
GROQ_API_KEY=gsk-...
ELASTIC_URL=http://localhost:9200
ELASTIC_INDEX=recipes
CHROMA_HOST=localhost
CHROMA_PORT=8000
NUTRITION_HOST=localhost
NUTRITION_PORT=5432
NUTRITION_DB=...
NUTRITION_USER=...
NUTRITION_PASSWORD=...
```

### 4. Run migrations

```bash
PYTHONPATH=src alembic upgrade head
```

### 5. Start API

```bash
PYTHONPATH=src uvicorn recipe_wrangler.api.main:app --reload --port 8001
```

Swagger UI: http://localhost:8001/docs

---

## Repository Structure

```
src/recipe_wrangler/
├── api/               # FastAPI app, routers, config, dependencies
├── tools/             # LangGraph pipelines and tool implementations
├── repositories/      # Data access layer (Neo4j, Postgres, Chroma)
├── utils/             # DB clients, embedding helpers, scoring utilities
└── schemas/           # Pydantic models

preprocessing/         # One-time data import scripts (not part of the API)
notebooks/             # End-to-end tool tests
*-docker/              # Docker Compose files per service
alembic/               # Database migrations
```

---

## Smoke Test

```bash
BASE=http://localhost:8001

curl -sS "$BASE/health"

curl -sS "$BASE/api/v1/recipes/autocomplete?q=pasta"

curl -sS -X POST "$BASE/api/v1/recipes/search" \
  -H "Content-Type: application/json" \
  -d '{"question": "quick chicken pasta", "exclude_allergens": []}'

curl -sS -X POST "$BASE/api/v1/recipes/param_search" \
  -H "Content-Type: application/json" \
  -d '{"include_ingredients": ["chicken"], "max_duration_minutes": 30}'

curl -sS -X POST "$BASE/api/v1/recipes/profile" \
  -H "Content-Type: application/json" \
  -d '{"raw_recipe": "Pasta\n200g spaghetti\n100g tomatoes\nBoil pasta. Add sauce.", "region": "IE"}'

curl -sS -X POST "$BASE/api/v1/recipes/" \
  -H "Content-Type: application/json" \
  -d '{"title": "Omelette", "ingredients": ["2 eggs", "10g butter"], "serves": 1, "duration": 5, "region": "IE"}'

# Use the recipe_id returned by create above
curl -sS -X PATCH "$BASE/api/v1/recipes/{recipe_id}" \
  -H "Content-Type: application/json" \
  -d '{"instructions": ["Beat eggs", "Cook in butter", "Fold and serve"]}'

curl -sS -X POST "$BASE/api/v1/recipes/{recipe_id}/substitute" \
  -H "Content-Type: application/json" \
  -d '{"ingredient": "butter", "region": "IE"}'
```

---

## Code Cleanup Recommendations

The codebase has grown organically and has several areas worth tidying before sharing with the team.

### High priority

**1. Delete `tools/text2cypher.py` (v1)**
`text2cypher_v2.py` is in production. v1 uses a different LLM provider and a different architecture. It is not called anywhere in the active API. Remove it to avoid confusion.

**2. Delete `utils/nutrition_postgres.py` (v1)**
This version uses `subprocess` to call `psql` directly. `nutrition_postgres_v2.py` uses SQLAlchemy and is what the API actually uses. Remove v1.

**3. Rename files to drop version suffixes**
Once v1 files are removed, rename `text2cypher_v2.py` → `text2cypher.py` and `nutrition_postgres_v2.py` → `nutrition_postgres.py` and update all imports accordingly.

**4. Add `.env.example`**
There is no `.env.example` in the repo. Add one with all required and optional keys (values blanked out). This is essential for onboarding.

**5. Verify `.gitignore` covers data files**
Confirm `chroma_db/`, `*.dump`, `*.sqlite`, `data/`, `qdrant_storage/`, `*.pkl`, `*.csv` (in data dirs) are all ignored. A `nutrients_postgres_2026-02-16.dump` and `tmp_recipe_images.sqlite` are currently untracked — do not commit these.

### Medium priority

**6. Move `preprocessing/` scripts out of the main repo (or into a subdir)**
These are one-time data pipeline scripts, not part of the running service. Consider moving them to a separate repo or a clearly labelled `scripts/data-prep/` folder with its own README explaining when each script runs and in what order.

**7. Split `routers/recipes.py` (960 lines)**
This file mixes routing, business logic, and normalisation. Extract:
- Normalisation helpers → `utils/normalisation.py`
- Fallback search logic → a service layer function
- Endpoint handlers should only call service functions and return responses

**8. Remove `utils/user_preferences.py` if it is a stub**
If this file only contains placeholder code, delete it. Stub files add noise when reading the codebase.

**9. Clean up `preprocessing/` `__init__.py` filename**
`__.init.__.py` is a typo — it is not a valid Python package init file. Either rename it to `__init__.py` or delete it.

### Low priority / nice to have

**10. Add a `CONTRIBUTING.md`** with setup steps, branch naming, and PR expectations.

**11. Pin the Groq model name in config** rather than using `openai/gpt-oss-20b` as the default, which is confusing (it is routed through Groq's OpenAI-compatible API but reads like an OpenAI model).

**12. Add type hints to repository functions** — `repositories/` is the newest layer and is the right place to enforce this going forward.
