"""Generate missing recipe images through Grokified and publish them locally.

The job is deliberately resumable. It selects only recipes whose Neo4j
``image_url`` is empty, gives each recipe a deterministic output filename and
idempotency key, writes the returned bytes atomically, then updates Neo4j and
reprojects the recipe into Elasticsearch.

Examples:

    # Inspect the pending work without making paid calls or writes.
    uv run python scripts/generate_recipe_images_grokified.py

    # Generate one image as a smoke test.
    uv run python scripts/generate_recipe_images_grokified.py --apply --limit 1

    # Resume all remaining Hungarian and Slovenian recipes.
    uv run python scripts/generate_recipe_images_grokified.py --apply
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from neo4j import GraphDatabase


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "generated_recipe_images" / "grokified"
DEFAULT_LOG_PATH = REPO_ROOT / "artifacts" / "grokified_image_generation.jsonl"
DEFAULT_SOURCES = ("Curated Hungarian Recipes", "Curated Slovenian Recipes")
DEFAULT_MODEL = "grok-imagine-image"
API_URL = "https://api.grokified.com/v1/images/generations"
IMAGE_URL_PREFIX = "/static/data/generated_recipe_images/grokified"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

logger = logging.getLogger("generate_recipe_images_grokified")


class GenerationError(RuntimeError):
    """A Grokified request failed after applying the retry policy."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    extension: str
    usage: dict[str, Any]
    request_id: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Generate files and update Neo4j/Elasticsearch. The default is a dry run.",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Recipe source to process. Repeat for multiple sources.",
    )
    parser.add_argument("--limit", type=int, help="Maximum recipes to process.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument(
        "--delay",
        type=float,
        default=6.5,
        help="Minimum seconds between generation calls (default: 6.5).",
    )
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument(
        "--skip-projection",
        action="store_true",
        help="Update Neo4j but do not reproject changed recipes to Elasticsearch.",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
    return args


def load_api_key() -> tuple[str, str]:
    """Return the configured key and its variable name without logging it."""
    for name in (
        "GROKIFIED_API_KEY",
        "grokified_api_key",
        # Compatibility with the local configuration used by the paused job.
        "grokified_api_key88",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value, name
    raise RuntimeError(
        "Set GROKIFIED_API_KEY in .env before running with --apply."
    )


def fetch_missing_recipes(
    driver: Any,
    *,
    sources: Iterable[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    query = """
        MATCH (r:Recipe)
        WHERE r.source IN $sources
          AND (r.image_url IS NULL OR trim(toString(r.image_url)) = '')
        OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
        WITH r, [name IN collect(DISTINCT i.name)
                 WHERE name IS NOT NULL AND trim(toString(name)) <> ''][..10]
                 AS ingredients
        RETURN r.recipe_id AS recipe_id,
               r.title AS title,
               r.source AS source,
               ingredients
        ORDER BY source, recipe_id
    """
    params: dict[str, Any] = {"sources": list(sources)}
    if limit is not None:
        query += " LIMIT $limit"
        params["limit"] = limit
    with driver.session() as session:
        return [dict(row) for row in session.run(query, **params)]


def recipe_filename(recipe_id: str, extension: str) -> str:
    """Return a readable, path-safe and collision-resistant filename."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", recipe_id).strip("-._") or "recipe"
    digest = hashlib.sha256(recipe_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:80]}-{digest}.{extension}"


def build_prompt(recipe: dict[str, Any]) -> str:
    title = str(recipe.get("title") or "Untitled recipe").strip()
    ingredients = [
        str(value).strip()
        for value in recipe.get("ingredients") or []
        if str(value).strip()
    ]
    ingredient_text = ", ".join(ingredients[:10])
    return (
        "Professional editorial food photography for a recipe catalog. "
        f"Finished dish: {title}. "
        + (f"Visible key ingredients: {ingredient_text}. " if ingredient_text else "")
        + "Show one realistic, appetising serving of the finished recipe, accurately "
        "matching the dish and ingredients. Natural plating, soft daylight, subtle "
        "shadows, three-quarter overhead view, square composition, high detail. "
        "No people, hands, packaging, labels, text, logos, borders, or watermark."
    )


def idempotency_key(recipe_id: str, model: str) -> str:
    raw = f"recipe-wrangler:{model}:{recipe_id}".encode("utf-8")
    return "rw-img-" + hashlib.sha256(raw).hexdigest()


def image_format(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    raise GenerationError("Grokified returned an unrecognized image format")


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)[:500]
    return str(payload)[:500]


def generate_image(
    session: requests.Session,
    *,
    api_key: str,
    recipe_id: str,
    prompt: str,
    model: str,
    max_attempts: int,
) -> GeneratedImage:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key(recipe_id, model),
    }
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }

    response: requests.Response | None = None
    for attempt in range(max_attempts):
        try:
            response = session.post(API_URL, headers=headers, json=body, timeout=180)
        except requests.RequestException as exc:
            if attempt == max_attempts - 1:
                raise GenerationError(f"network error: {exc}") from exc
        else:
            if response.ok:
                payload = response.json()
                rows = payload.get("data") or []
                encoded = rows[0].get("b64_json") if rows else None
                if not encoded:
                    raise GenerationError("successful response did not contain data[0].b64_json")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise GenerationError("response contained invalid base64 image data") from exc
                return GeneratedImage(
                    content=content,
                    extension=image_format(content),
                    usage=payload.get("usage") or {},
                    request_id=response.headers.get("x-request-id"),
                )

            retryable = response.status_code in RETRYABLE_STATUSES
            if not retryable or attempt == max_attempts - 1:
                raise GenerationError(
                    _error_message(response), status_code=response.status_code
                )

        backoff = min(2**attempt, 30)
        if response is not None and response.headers.get("Retry-After"):
            try:
                backoff = max(backoff, int(response.headers["Retry-After"]))
            except ValueError:
                pass
        wait = backoff + random.random() * min(backoff * 0.25, 2.0)
        logger.warning("retrying %s in %.1fs", recipe_id, wait)
        time.sleep(wait)

    raise AssertionError("retry loop exited unexpectedly")


def write_image(output_dir: Path, recipe_id: str, image: GeneratedImage) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / recipe_filename(recipe_id, image.extension)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(image.content)
    temporary.replace(path)
    return path


def existing_image(output_dir: Path, recipe_id: str) -> Path | None:
    stem = recipe_filename(recipe_id, "").rstrip(".")
    matches = sorted(output_dir.glob(stem + ".*"))
    return next((path for path in matches if path.suffix != ".part"), None)


def publish_image_url(driver: Any, recipe_id: str, image_url: str) -> bool:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (r:Recipe {recipe_id: $recipe_id})
            WHERE r.image_url IS NULL OR trim(toString(r.image_url)) = ''
            SET r.image_url = $image_url, r.updated_at = datetime()
            RETURN count(r) AS updated
            """,
            recipe_id=recipe_id,
            image_url=image_url,
        ).single(strict=True)
    return bool(row["updated"])


def image_url(path: Path) -> str:
    return f"{IMAGE_URL_PREFIX}/{path.name}"


def append_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def project_recipe(recipe_id: str) -> None:
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    from recipe_wrangler.catalog.projection import project

    project(recipe_id)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(REPO_ROOT / ".env")

    sources = tuple(args.sources or DEFAULT_SOURCES)
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "neo4j")),
        connection_timeout=10,
    )
    try:
        recipes = fetch_missing_recipes(driver, sources=sources, limit=args.limit)
        logger.info("pending recipes: %d", len(recipes))
        for recipe in recipes:
            logger.info(
                "pending %s | %s | %s",
                recipe["recipe_id"],
                recipe["source"],
                recipe["title"],
            )
        if not args.apply or not recipes:
            return 0

        api_key, key_name = load_api_key()
        if key_name != "GROKIFIED_API_KEY":
            logger.warning("using legacy API-key variable %s", key_name)

        http = requests.Session()
        completed = 0
        failed = 0
        total_charged = 0.0
        for position, recipe in enumerate(recipes, start=1):
            recipe_id = str(recipe["recipe_id"])
            title = str(recipe.get("title") or "Untitled recipe")
            prompt = build_prompt(recipe)
            path = existing_image(args.output_dir, recipe_id)
            generated: GeneratedImage | None = None
            started = time.monotonic()
            try:
                if path is None:
                    logger.info("[%d/%d] generating %s", position, len(recipes), title)
                    generated = generate_image(
                        http,
                        api_key=api_key,
                        recipe_id=recipe_id,
                        prompt=prompt,
                        model=args.model,
                        max_attempts=args.max_attempts,
                    )
                    path = write_image(args.output_dir, recipe_id, generated)
                else:
                    logger.info("[%d/%d] recovering existing %s", position, len(recipes), path.name)

                url = image_url(path)
                updated = publish_image_url(driver, recipe_id, url)
                projected = False
                projection_error: str | None = None
                if not args.skip_projection:
                    try:
                        project_recipe(recipe_id)
                        projected = True
                    except Exception as exc:  # noqa: BLE001 - preserve generated asset
                        projection_error = str(exc)
                        logger.warning("projection failed for %s: %s", recipe_id, exc)

                usage = generated.usage if generated else {}
                charged = float((usage.get("grokified") or {}).get("charged_usd") or 0.0)
                total_charged += charged
                append_log(
                    args.log_path,
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "completed",
                        "recipe_id": recipe_id,
                        "source": recipe.get("source"),
                        "title": title,
                        "model": args.model,
                        "prompt": prompt,
                        "path": str(path.relative_to(REPO_ROOT)),
                        "image_url": url,
                        "neo4j_updated": updated,
                        "projected": projected,
                        "projection_error": projection_error,
                        "request_id": generated.request_id if generated else None,
                        "usage": usage,
                    },
                )
                completed += 1
                logger.info(
                    "[%d/%d] completed %s (charged $%.4f; total $%.4f)",
                    position,
                    len(recipes),
                    recipe_id,
                    charged,
                    total_charged,
                )
            except GenerationError as exc:
                failed += 1
                logger.error("[%d/%d] failed %s: %s", position, len(recipes), recipe_id, exc)
                append_log(
                    args.log_path,
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "failed",
                        "recipe_id": recipe_id,
                        "source": recipe.get("source"),
                        "title": title,
                        "model": args.model,
                        "prompt": prompt,
                        "http_status": exc.status_code,
                        "error": str(exc),
                    },
                )
                # An exhausted rate limit is normally a daily account limit.
                # Stop cleanly so the remaining recipes stay eligible to resume.
                if exc.status_code == 429:
                    logger.error("rate limit remained active after retries; stopping resumably")
                    break
            elapsed = time.monotonic() - started
            if position < len(recipes) and elapsed < args.delay:
                time.sleep(args.delay - elapsed)

        logger.info(
            "run finished: completed=%d failed=%d charged=$%.4f remaining_at_start=%d",
            completed,
            failed,
            total_charged,
            len(recipes) - completed,
        )
        return 1 if failed else 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
