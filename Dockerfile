# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    TMPDIR=/mnt/tmp \
    PORT=8001 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_CACHE_DIR=/mnt/cache/pip

RUN mkdir -p /mnt/tmp /mnt/cache/pip

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY scripts/deployment/matching-runtime-constraints.txt ./matching-runtime-constraints.txt
COPY README.md ./README.md
COPY src/recipe_wrangler/__init__.py ./src/recipe_wrangler/__init__.py

RUN --mount=type=cache,target=/mnt/cache/pip \
    pip install --upgrade pip setuptools wheel \
    && pip install --no-compile torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-compile -c matching-runtime-constraints.txt .

COPY src/ ./src/

# Versioned parser/ontology inputs required by the matching adapter; recipe data
# and research output directories are not copied wholesale into the API image.
COPY data/nutrition/matching/dictionary/ingredient_dictionary_2026-10-22/foodon_dictionary.json ./data/nutrition/matching/dictionary/ingredient_dictionary_2026-10-22/foodon_dictionary.json
COPY data/nutrition/matching/vocabularies/derived/matching_vocabularies_2026-10-21/foodon_named_parents.csv ./data/nutrition/matching/vocabularies/derived/matching_vocabularies_2026-10-21/foodon_named_parents.csv
COPY data/nutrition/matching/vocabularies/derived/foodex2/17.2/facets.json ./data/nutrition/matching/vocabularies/derived/foodex2/17.2/facets.json
COPY data/nutrition/matching/vocabularies/derived/langual/2017/terms.json ./data/nutrition/matching/vocabularies/derived/langual/2017/terms.json
COPY data/nutrition/matching/vocabularies/raw/matching_vocabularies_2026-10-21/MTX.ecf ./data/nutrition/matching/vocabularies/raw/matching_vocabularies_2026-10-21/MTX.ecf
ENV NUTRITION_MATCHING_ASSET_ROOT=/app

# The build helper copies the published revision from the local verified cache.
COPY matching-model-cache/ ./matching-model-cache/
ENV HF_HOME=/app/matching-model-cache HF_HUB_OFFLINE=1

# Reinstall only the local package after source changes; dependencies stay cached.
RUN --mount=type=cache,target=/mnt/cache/pip \
    pip install --no-compile --no-deps .

EXPOSE 8001

# Localhost only (the API runs with network_mode: host). Never bind 0.0.0.0 on the shared server; reach it via ssh -L.
CMD ["python", "-m", "uvicorn", "recipe_wrangler.api.main:app", "--host", "127.0.0.1", "--port", "8001"]
