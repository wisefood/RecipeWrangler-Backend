# RecipeWrangler API

FastAPI entrypoint: `recipe_wrangler.api.main:app`. Runs on port 8001 by default to avoid clashing with Chroma (8000).

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
- `GROQ_API_KEY` for LLM-backed tools
- `CHROMA_HOST`/`CHROMA_PORT` if different from defaults (localhost:8000)

## Endpoints
- `GET /health` — readiness probe
- `GET /api/v1/recipes/{recipe_id}` — fetch recipe metadata by recipe_id
- `POST /api/v1/recipes/search` — LangGraph search over the recipe graph; returns results + cypher_statement + steps
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
Standalone app, not mounted on main backend by default.

Run:
```bash
PYTHONPATH=src uvicorn recipe_wrangler.services.adaptation.app:app --reload --port 8101
```

Endpoints:
- `POST /api/v1/recipes/{recipe_id}/adapt/suggestions` — recommend swaps or quantity reduction for worst offender
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
```
