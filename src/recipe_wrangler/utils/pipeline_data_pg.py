# Purpose: Load pipeline static data files from Postgres (pipeline_static_data table).
# Falls back to local file if provided. All loads are cached with lru_cache.

import json
import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_postgres import get_connection

logger = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def load_pipeline_data(name: str):
    """Load a pipeline static data entry by name from Postgres. Cached per process."""
    with get_connection() as conn:
        row = conn.execute(
            text('SELECT content FROM pipeline_static_data WHERE name = :name'),
            {"name": name},
        ).fetchone()
    if row is None:
        raise KeyError(f"pipeline_static_data: no entry for '{name}'")
    content = row[0]
    # JSONB comes back already deserialized; handle both cases.
    if isinstance(content, str):
        return json.loads(content)
    return content


def load_pipeline_data_or_file(name: str, path, jsonl: bool = False):
    """Postgres first; the local file only when the DB or the entry is unavailable (logged)."""
    try:
        return load_pipeline_data(name)
    except Exception as exc:
        logger.warning("pipeline_static_data '%s' unavailable (%s); reading %s", name, exc, path)
    path = Path(path)
    if not path.exists():
        return [] if jsonl else {}
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()] if jsonl else json.loads(text)
