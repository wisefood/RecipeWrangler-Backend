# Use official Python runtime as base image
FROM python:3.10-slim

# Set working directory in container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Create directory for .env file (optional, can be mounted)
RUN mkdir -p /app/src/recipe_wrangler/api

# Expose port (default FastAPI port)
EXPOSE 8001

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8001

# Run the FastAPI application
CMD ["python", "-m", "uvicorn", "recipe_wrangler.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
