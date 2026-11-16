# Recipe Wrangler backend

Recipe Wrangler analyzes recipes (parsing, ingredient weights, food-composition matching, nutrition, allergens, diets, emissions, cost), serves a regional recipe catalogue for search, and develops evidence-backed recipe adaptations.

| Component | Role |
|---|---|
| FastAPI (`src/recipe_wrangler/api/`) | HTTP API, port 8001 |
| PostgreSQL | Composition tables, prices, versioned recipe profiles |
| Neo4j | Recipe graph, ingredient identities and facets |
| Elasticsearch | Search projections and reference indexes |

## Development

```bash
uv sync --dev
make api          # http://localhost:8001/docs
uv run pytest
```

Configure `.env` from `.env.example` and start the backing services with `docker compose --env-file .env -f <service>-docker/docker-compose.y(a)ml up -d` (`neo4j-docker/`, `postgresql-docker/`, `elasticsearch-docker/`). Services bind to localhost only. See [OPERATIONS](docs/OPERATIONS.md) before importing data or deploying, and the local `AGENTS.md` (not in Git) for resource limits on the shared server.

## Where to look

| Need | Go to |
|---|---|
| Current objective, decisions, next steps | [docs/PLAN.md](docs/PLAN.md) |
| How analysis works (parsing, weights, nutrition, allergens, time) | [docs/ANALYSIS.md](docs/ANALYSIS.md) |
| Ingredient-to-composition matching | [docs/MATCHING.md](docs/MATCHING.md) |
| Recipe adaptation | [docs/ADAPTATION.md](docs/ADAPTATION.md) |
| Cost | [docs/COST.md](docs/COST.md) |
| Data sources and provenance | [docs/DATA.md](docs/DATA.md) |
| Setup, architecture, activation, rollback | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Partner and Living Lab feedback | [docs/feedback/](docs/feedback/) |
| Evaluation data, results and methods | [evaluation/](evaluation/README.md) |
| Scripts and import order | [scripts/README.md](scripts/README.md), [scripts/import/README.md](scripts/import/README.md) |
| API and service modules | [api/README.md](src/recipe_wrangler/api/README.md), [services/README.md](src/recipe_wrangler/services/README.md) |

`analysis/` holds local, gitignored run outputs. Docs do not link into it; results worth sharing are copied into `evaluation/`.
