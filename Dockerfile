# syntax=docker/dockerfile:1.7

FROM python:3.10

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

COPY pyproject.toml README.md ./
COPY src/recipe_wrangler/__init__.py ./src/recipe_wrangler/__init__.py

RUN --mount=type=cache,target=/mnt/cache/pip \
    pip install --upgrade pip setuptools wheel \
    && pip install --no-compile torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-compile .

COPY src/ ./src/

# Reinstall only the local package after source changes; dependencies stay cached.
RUN --mount=type=cache,target=/mnt/cache/pip \
    pip install --no-compile --no-deps .

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "recipe_wrangler.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
