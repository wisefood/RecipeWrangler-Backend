DOCKER=docker
IMGTAG=wisefood/recipe-wrangler:latest
HOST ?= 127.0.0.1
PORT ?= 8001
MATCHING_RELEASE ?= 2026-10-27-v11

.PHONY: all build push help api dumps dump-all dump-neo4j dump-postgres dump-elasticsearch dump-assets tag-dish-types

TIMESTAMP ?= $(shell date -u +%Y%m%d_%H%M%S)

all: build push

build:
	PYTHONPATH=src uv run python -m scripts.deployment.build_matching_image --release-id $(MATCHING_RELEASE) --tag $(IMGTAG)

push:
	$(DOCKER) push $(IMGTAG)

help:
	@printf '%s\n' \
	  'Build/push:' \
	  '  make build' \
	  '  make push' \
	  '' \
	  'Run API locally:' \
	  '  make api' \
	  '  make api PORT=8002' \
	  '' \
	  'Timestamped sync dumps under data_to_send/dumps/<timestamp>/:' \
	  '  make dumps   (stop API, dump all stores to dumps/sent/<YYYY-MM-DD>, restart)' \
	  '  make dump-all' \
	  '  make dump-neo4j' \
	  '  make dump-postgres' \
	  '  make dump-elasticsearch' \
	  '  make dump-assets' \
	  '' \
	  'Tag dish types in Neo4j using the configured vLLM model:' \
	  '  make tag-dish-types' \
	  '  make tag-dish-types BATCH_SIZE=200 LIMIT=1000' \
	  '' \
	  'Optional override:' \
	  '  make dump-all TIMESTAMP=20260423_160000'

api:
	PYTHONPATH=src uv run uvicorn recipe_wrangler.api.main:app --reload --host "$(HOST)" --port "$(PORT)"

API_CONTAINER ?= recipe-wrangler-api
DUMP_DATE ?= $(shell date +%F)

# Neo4j is stopped and restarted by export_sync_bundle.py itself (a native dump needs it stopped).
# The API is stopped so nothing writes during the dump, and restarted even if the dump fails.
dumps:
	./scripts/maintenance/make_dumps.sh "$(API_CONTAINER)" "$(DUMP_DATE)"

dump-all:
	uv run python scripts/export_sync_bundle.py --timestamp "$(TIMESTAMP)"

dump-neo4j:
	uv run python scripts/export_sync_bundle.py --components neo4j --timestamp "$(TIMESTAMP)"

dump-postgres:
	uv run python scripts/export_sync_bundle.py --components postgres --timestamp "$(TIMESTAMP)"

dump-elasticsearch:
	uv run python scripts/export_sync_bundle.py --components elasticsearch --timestamp "$(TIMESTAMP)"

dump-assets:
	uv run python scripts/export_sync_bundle.py --components assets --timestamp "$(TIMESTAMP)"

BATCH_SIZE ?= 100
LIMIT ?= 0

tag-dish-types:
	PYTHONPATH=src uv run python scripts/neo4j/tag_dish_types_vllm.py --batch-size "$(BATCH_SIZE)" --limit "$(LIMIT)"
