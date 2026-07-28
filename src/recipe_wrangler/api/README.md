# RecipeWrangler API

FastAPI entrypoint: `recipe_wrangler.api.main:app`. Runs on port 8001 by default.

## Start the API
```bash
# activate your venv first
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True   # helps avoid CUDA fragmentation/OOM
PYTHONPATH=src uvicorn recipe_wrangler.api.main:app --reload --port 8001
# or
python -m recipe_wrangler.api.main  # also defaults to port 8001
```
Swagger UI: http://127.0.0.1:8001/docs

Environment:
- `NEO4J_URI` (and `NEO4J_USERNAME`/`NEO4J_PASSWORD` if auth)
- `SEARCH_LLM_SOURCE`, `SEARCH_MAIN_MODEL`, and the selected provider API key
  for natural-language recipe-search routing
- `ELASTIC_URL` and `ELASTIC_VECTOR_INDEX` for vector search

## Endpoints
- `GET /health` — readiness probe
- `GET /api/v1/recipes/{recipe_id}` — fetch recipe metadata by recipe_id
- `POST /api/v1/recipes/search` — intent-aware Elasticsearch recipe search
- `POST /api/v1/recipes/profile` — run the parsing + profiling chain on raw recipe text (may be GPU-heavy)
- `POST /api/v1/recipes/{recipe_id}/substitute` — swap one ingredient using Neo4j substitution graph; returns either recalculated profile or fallback modified ingredient payload

## Substitution Mechanism
- Load recipe from Neo4j.
- Confirm requested ingredient exists.
- Find candidates from `HAS_SUBSTITUTION` edges first, then FoodOn taxonomy fallback.
- Replace ingredient name, keep original measurements.
- Try structured profiling chain on modified recipe.
- If profiling stack unavailable or too slow, return `modified_recipe_profile.status="profiling_unavailable"` instead of `503`.

## Experimental Adaptation Service
Mounted on the main backend and also runnable as a standalone app.

Run:
```bash
PYTHONPATH=src uvicorn recipe_wrangler.services.adaptation.app:app --reload --port 8101
```

Endpoints:
- `POST /api/v1/recipes/{recipe_id}/adapt/suggestions` — recommend nutrition,
  sustainability, quantity-reduction, vegan-composition, or
  vegetarian-composition changes. Consumer-group suggestions are restricted
  to recipe-used Neo4j ingredients with explicit suitability and valid
  regional nutrition, and include a fully recalculated adapted recipe preview.
  The currently supported consumer groups are `vegan` and `vegetarian`.
  Elasticsearch proposes alternatives; FATO-aligned Neo4j
  `SUITABILITY_FOR` evidence is the mandatory eligibility check before the
  service recalculates nutrition and returns an adapted recipe.
- `POST /api/v1/recipes/{recipe_id}/adapt/simulate` — simulate one exact swap and return before/after deltas

## Curl examples
```bash
BASE="${BASE:-http://127.0.0.1:8001}"

# -sS keeps output clean but still shows request errors.
curl -sS "$BASE/health"; echo

curl -sS -X GET "$BASE/api/v1/recipes/123"; echo

curl -sS -X POST "$BASE/api/v1/recipes/search" \
  -H "Content-Type: application/json" \
  -d '{"question":"chicken and rice under 30 minutes","exclude_allergens":["peanut"]}'; echo

curl -sS -X POST "$BASE/api/v1/recipes/profile" \
  -H "Content-Type: application/json" \
  -d '{"raw_recipe":"Garlic Butter Shrimp...","region":"US"}'; echo

curl -sS -X POST \
  "$BASE/api/v1/recipes/020b17b247/adapt/suggestions" \
  -H "Content-Type: application/json" \
  -d '{"region":"IE","mode":"vegan","max_swaps":3,"use_llm":false}'; echo

curl -sS -X POST \
  "$BASE/api/v1/recipes/017f92f1c3/adapt/suggestions" \
  -H "Content-Type: application/json" \
  -d '{"region":"IE","mode":"vegetarian","max_swaps":1,"use_llm":false}'; echo
```
