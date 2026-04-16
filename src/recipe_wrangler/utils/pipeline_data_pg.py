# Purpose: Load pipeline static data files from Postgres (pipeline_static_data table).
# Falls back to local file if provided. All loads are cached with lru_cache.

import json
import os
from functools import lru_cache

import psycopg2


def _get_conn():
    return psycopg2.connect(
        host=os.environ["NUTRITION_HOST"],
        port=int(os.environ.get("NUTRITION_PORT", 5432)),
        dbname=os.environ["NUTRITION_DB"],
        user=os.environ["NUTRITION_USER"],
        password=os.environ["NUTRITION_PASSWORD"],
    )


@lru_cache(maxsize=32)
def load_pipeline_data(name: str):
    """Load a pipeline static data entry by name from Postgres. Cached per process."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT content FROM pipeline_static_data WHERE name = %s',
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"pipeline_static_data: no entry for '{name}'")
        content = row[0]
        # psycopg2 returns JSONB already deserialized; handle both cases.
        if isinstance(content, str):
            return json.loads(content)
        return content
    finally:
        conn.close()
