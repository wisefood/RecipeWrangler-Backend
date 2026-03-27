FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir "numpy<2.0.0" \
 && pip install --no-cache-dir "chromadb==0.4.24"

# Copy prebuilt Chroma data into the image (assumes chroma_db exists in build context)
COPY chroma_db /chroma-init

# Expose the API port
EXPOSE 8000

# Run the Chroma server using the CLI (supports --path)
CMD ["chroma", "run", "--path", "/chroma", "--host", "0.0.0.0", "--port", "8000"]
