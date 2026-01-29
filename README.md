# RecipeWrangler Backend

LangGraph/LangChain-backed tools and FastAPI endpoints for parsing recipes, profiling nutrition/sustainability, and querying a Neo4j recipe graph. Chroma and Neo4j run via Docker; a notebook exercises every tool end-to-end.

## What lives where
- `src/recipe_wrangler/api/` – FastAPI app (`main.py`) and schemas.
- `src/recipe_wrangler/tools/` – LangChain/LangGraph tools (text2cypher, parsing, profiling, embeddings, weighting, tagging, fetch_recipe_info).
- `src/recipe_wrangler/utils/` – Chroma/Neo4j helpers, embedding utilities, and data loaders.
- `preprocessing/` – notebooks/scripts for cleaning and preparing raw data before loading.
- `graph/` – notebooks (e.g., DataToGraph) with the steps to populate the recipe graph.
- `notebooks/test_tools.ipynb` – Notebook that hits every major tool once the services are up.
- `chromadb-docker/` – Docker Compose + README to run a Chroma server with baked-in collections.
- `neo4j-docker/` – Docker Compose for Neo4j (bring your own dump if needed).
- `data/`, `chroma_db/`, `qdrant_storage/` – gitignored data stores; regenerate or download before running.

## Bring the services up first
1) Start Chroma (with bundled data):
```bash
docker compose -f chromadb-docker/docker-compose.yml up -d
```
   - Chroma listens on `http://localhost:8000`. See `chromadb-docker/README.md` if you need to rebuild with new data.

2) Start Neo4j:
```bash
docker compose -f neo4j-docker/docker-compose.yml up -d
```
   - Set `NEO4J_URI` (plus `NEO4J_USERNAME`/`NEO4J_PASSWORD` if auth) in your environment.

## Run the API locally
```bash
uv venv
source .venv/bin/activate
uv pip install -e .
# optional: uv lock && uv sync
export NEO4J_URI=bolt://localhost:7687  # adjust if needed
export OPENAI_API_KEY=...               # required for LLM-backed tools
PYTHONPATH=src uvicorn recipe_wrangler.api.main:app --reload --port 8001  # 8000 is used by Chroma
```
Swagger UI: http://127.0.0.1:8000/docs

Or install the package in editable mode so `recipe_wrangler` imports work everywhere without `PYTHONPATH`:
```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```
Then run scripts/notebooks directly (they resolve imports via the editable install).

Quick alt: `python -m recipe_wrangler.api.main` also starts the API on port 8001 by default.

- For GPU runs, set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` before starting uvicorn to avoid CUDA OOM fragmentation.
- See `src/recipe_wrangler/api/README.md` for endpoint-by-endpoint curl examples and details.

## Example .env
```
NEO4J_URI=bolt://localhost:7687
# NEO4J_USERNAME=neo4j
# NEO4J_PASSWORD=secret
OPENAI_API_KEY=sk-...
# Optional overrides:
# CHROMA_HOST=localhost
# CHROMA_PORT=8000
```

## Test all tools via the notebook
- Open `notebooks/test_tools.ipynb` after the Chroma and Neo4j containers are running.
- The cells call every major tool (parsing, embeddings, sustainability, nutrition, profiling, cypher generation, recipe info) against the live services.
- The notebook uses the repo root `chroma_db/` as the persist path and reads `NEO4J_URI` from the environment; no secrets are baked into the file.

## Data & stores
- Large artifacts (`chroma_db/`, `qdrant_storage/`, CSV/PKL/JSONL exports) are gitignored. Rebuild collections with the scripts in `src/recipe_wrangler/services/` or use the prebuilt Chroma image.
- Keep secrets in `.env`; `.env` files are ignored.

## Notes
- All Python code lives under `src/recipe_wrangler/`; set `PYTHONPATH=src` or install in editable mode (`uv pip install -e .`).
- If you add new pipelines or data scripts, place them under `tools/`, `utils/`, or `services/` to keep the layout tidy.
