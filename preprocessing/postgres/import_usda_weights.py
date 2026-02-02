import json
import os
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

# Purpose: Load USDA weights JSON into Postgres.

DB_NAME = os.getenv("NUTRITION_DB", os.getenv("USDA_DB", "nutrients"))
DB_USER = os.getenv("NUTRITION_USER", os.getenv("USDA_USER", "postgres"))
DB_PASSWORD = os.getenv("NUTRITION_PASSWORD", os.getenv("USDA_PASSWORD", "postgres"))
DB_HOST = os.getenv("NUTRITION_HOST", os.getenv("USDA_HOST", "localhost"))
DB_PORT = int(os.getenv("NUTRITION_PORT", os.getenv("USDA_PORT", "5432")))
SCHEMA_NAME = os.getenv("NUTRITION_SCHEMA", "usda")

DATA_PATH = Path("data/processed/usda/usda-weights-v1.json")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.weights (
  food_id  text PRIMARY KEY,
  portions jsonb NOT NULL
);
"""

INSERT_SQL = """
INSERT INTO {schema}.weights (food_id, portions)
VALUES %s
ON CONFLICT (food_id) DO NOTHING;
"""


def load_weights(path: Path):
    with path.open("r") as f:
        return json.load(f)


def main():
    items = load_weights(DATA_PATH)
    print("Total foods:", len(items))

    rows = [
        (
            item["food_id"],
            json.dumps(item["portions"]),
        )
        for item in items
    ]

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(SCHEMA_NAME)
                    )
                )
                cur.execute(
                    sql.SQL(CREATE_TABLE_SQL).format(schema=sql.Identifier(SCHEMA_NAME))
                )
                insert_sql = sql.SQL(INSERT_SQL).format(
                    schema=sql.Identifier(SCHEMA_NAME)
                )
                execute_values(cur, insert_sql, rows, page_size=1000)
    finally:
        conn.close()

    print("USDA weights import completed successfully.")


if __name__ == "__main__":
    main()
