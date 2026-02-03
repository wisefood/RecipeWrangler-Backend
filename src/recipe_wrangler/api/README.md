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

## Curl examples
```bash
curl http://127.0.0.1:8001/health

curl -X GET http://127.0.0.1:8001/api/v1/recipes/123

curl -X POST http://127.0.0.1:8001/api/v1/recipes/search \
  -H "Content-Type: application/json" \
  -d '{"question": "chicken and rice under 30 minutes"}'

curl -X POST http://127.0.0.1:8001/api/v1/recipes/profile \
  -H "Content-Type: application/json" \
  -d '{"raw_recipe": "Garlic Butter Shrimp..."}'
```
