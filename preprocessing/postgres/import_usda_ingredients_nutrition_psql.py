import json
import os
import subprocess
from pathlib import Path
from typing import Generator, Iterable

# Purpose: Load USDA ingredient nutrient JSON into Postgres.

DATA_PATH = Path("data/processed/usda/usda-nutrients-v1.json")

CONTAINER = os.getenv("POSTGRES_CONTAINER", "wisefood-postgres")
DB_NAME = os.getenv("NUTRITION_DB", "nutrition")
DB_USER = os.getenv("NUTRITION_USER", "postgres")
SCHEMA = os.getenv("NUTRITION_SCHEMA", "public")
TABLE = os.getenv("NUTRITION_INGREDIENTS_TABLE", "nutrients-ingredients-usda")

BATCH_SIZE = int(os.getenv("USDA_INGREDIENTS_IMPORT_BATCH_SIZE", "500"))
CHUNK_SIZE = int(os.getenv("USDA_INGREDIENTS_IMPORT_CHUNK_SIZE", "65536"))


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def quote_ident(value: str) -> str:
    return f'"{value.replace("\"", "\"\"")}"'


def iter_json_array(path: Path) -> Generator[dict, None, None]:
    decoder = json.JSONDecoder()
    buf = ""
    idx = 0
    started = False
    finished = False

    with path.open("r") as f:
        while not finished:
            chunk = f.read(CHUNK_SIZE)
            if chunk:
                buf += chunk
            else:
                finished = True

            while True:
                n = len(buf)
                while idx < n and buf[idx].isspace():
                    idx += 1
                if idx >= n:
                    break

                ch = buf[idx]
                if not started:
                    if ch != "[":
                        raise ValueError("Expected '[' at start of JSON array")
                    started = True
                    idx += 1
                    continue

                if ch == ",":
                    idx += 1
                    continue

                if ch == "]":
                    return

                try:
                    obj, next_idx = decoder.raw_decode(buf, idx)
                except json.JSONDecodeError:
                    buf = buf[idx:]
                    idx = 0
                    break

                yield obj
                idx = next_idx

            if idx > 0:
                buf = buf[idx:]
                idx = 0


def batch_iter(items: Iterable[dict], size: int) -> Generator[list[dict], None, None]:
    batch: list[dict] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def run_import() -> None:
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"Missing data file: {DATA_PATH}")

    cmd = [
        "docker",
        "exec",
        "-i",
        CONTAINER,
        "psql",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    assert proc.stdin is not None

    schema_sql = quote_ident(SCHEMA)
    table_sql = quote_ident(TABLE)
    table_fq = f"{schema_sql}.{table_sql}"

    proc.stdin.write(
        f"""
BEGIN;
CREATE TABLE IF NOT EXISTS {table_fq} (
  usda_id   text PRIMARY KEY,
  food_name text NOT NULL,
  nutrients jsonb NOT NULL
);
COMMIT;
"""
    )

    inserted = 0
    skipped = 0

    for batch in batch_iter(iter_json_array(DATA_PATH), BATCH_SIZE):
        values: list[str] = []
        for item in batch:
            usda_id = item.get("usda_id")
            food_name = item.get("food_name")
            nutrients = item.get("nutrients")

            if not usda_id or not food_name or nutrients is None:
                skipped += 1
                continue

            food_name_sql = sql_escape(str(food_name))
            nutrients_sql = sql_escape(json.dumps(nutrients, separators=(",", ":")))

            values.append(
                f"('{sql_escape(str(usda_id))}','{food_name_sql}','{nutrients_sql}'::jsonb)"
            )

        if not values:
            continue

        values_sql = ",\n".join(values)
        proc.stdin.write(
            f"""
BEGIN;
INSERT INTO {table_fq} (
  usda_id, food_name, nutrients
)
VALUES
{values_sql}
ON CONFLICT (usda_id) DO NOTHING;
COMMIT;
"""
        )
        inserted += len(values)

    proc.stdin.close()

    return_code = proc.wait()

    print(f"Attempted inserts: {inserted}")
    if skipped:
        print(f"Skipped rows: {skipped}")

    if return_code != 0:
        raise SystemExit(return_code)


if __name__ == "__main__":
    run_import()
