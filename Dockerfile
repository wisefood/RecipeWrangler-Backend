backup
FROM python:3.10

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/var/tmp
ENV PORT=8001

RUN apt-get update && apt-get install -y \
    gcc g++ git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --upgrade pip

# Install CPU-only torch FIRST
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install your app without re-pulling deps
RUN pip install . --no-deps

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "recipe_wrangler.api.main:app", "--host", "0.0.0.0", "--port", "8001"]



