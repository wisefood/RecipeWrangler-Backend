# Purpose: Load pipeline static data files from Postgres (pipeline_static_data table).
# Falls back to local file if provided. All loads are cached with lru_cache.

import json
from functools import lru_cache

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_postgres import get_connection


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
