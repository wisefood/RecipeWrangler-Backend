# Chroma with bundled collections

This Compose file runs a Chroma server that already includes the `chroma_db` data baked into the image. No host volume is mounted; the data lives inside `/chroma` in the container.

## Usage

1. Load the prebuilt image (if provided as a tar):
   ```bash
   docker load -i chromadb-with-data.tar
   ```
   The image tag must be `local/chromadb:0.4.24`.
2. Start Chroma:
   ```bash
   docker compose -f chromadb-docker/docker-compose.yml up -d
   ```
3. Access the API at `http://localhost:8000` (collections are already present).

## Updating data (optional)

If you regenerate collections and want a new image, rebuild (requires `chroma_db` present at build time), then save a tar to share:
```bash
docker compose -f chromadb-docker/docker-compose.yml build
docker compose -f chromadb-docker/docker-compose.yml up -d --force-recreate
docker save local/chromadb:0.4.24 -o chromadb-with-data.tar
```

## Notes

- The `chroma_db` folder is git-ignored. Only needed locally if you rebuild the image; not needed when running the prebuilt image.
- Container writes do not persist to the host. Rebuild the image after updating data.
