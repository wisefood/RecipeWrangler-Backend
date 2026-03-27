"""Import Hungarian composition table CSV into Postgres table nutrients-ingredients-hungarian."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


if load_dotenv:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


REPO_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = Path(
    os.getenv(
        "HUNGARIAN_INGREDIENTS_CSV",
        REPO_ROOT / "data/processed/hungarian-comp-table/hungarian_comp_table.csv",
    )
)

DB_NAME = os.getenv("NUTRITION_DB", os.getenv("POSTGRES_DB", "rag"))
DB_USER = os.getenv("NUTRITION_USER", os.getenv("POSTGRES_USER", "rag"))
DB_PASSWORD = os.getenv("NUTRITION_PASSWORD", os.getenv("POSTGRES_PASSWORD", "rag"))
DB_HOST = os.getenv("NUTRITION_HOST", os.getenv("POSTGRES_HOST", "localhost"))
DB_PORT = int(os.getenv("NUTRITION_PORT", os.getenv("POSTGRES_PORT", "5432")))
SCHEMA = os.getenv("NUTRITION_SCHEMA", "public")
TABLE = os.getenv("NUTRITION_INGREDIENTS_HUNGARIAN_TABLE", "nutrients-ingredients-hungarian")
BATCH_SIZE = int(os.getenv("HUNGARIAN_INGREDIENTS_IMPORT_BATCH_SIZE", "1000"))
TRUNCATE = os.getenv("HUNGARIAN_INGREDIENTS_TRUNCATE", "1") == "1"


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        if "canonical_food_id" not in headers or "Food Name" not in headers:
            raise RuntimeError(
                "CSV must contain at least canonical_food_id and Food Name columns."
            )
        rows = [row for row in reader]
    return headers, rows


def _connect():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )


def _ensure_table(cur, headers: list[str]) -> None:
    col_defs = [sql.SQL("row_id BIGSERIAL PRIMARY KEY")]
    for h in headers:
        col_defs.append(sql.SQL("{} TEXT").format(sql.Identifier(h)))

    create_stmt = sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
        sql.Identifier(SCHEMA),
        sql.Identifier(TABLE),
        sql.SQL(", ").join(col_defs),
    )
    cur.execute(create_stmt)
    cur.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
            sql.Identifier(f"{TABLE}_canonical_idx"),
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
            sql.Identifier("canonical_food_id"),
        )
    )


def _truncate(cur) -> None:
    cur.execute(
        sql.SQL("TRUNCATE TABLE {}.{} RESTART IDENTITY").format(
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
        )
    )


def _iter_batches(items: list[dict[str, str]], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _insert_rows(cur, headers: list[str], rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    inserted = 0
    insert_stmt = sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
        sql.Identifier(SCHEMA),
        sql.Identifier(TABLE),
        sql.SQL(", ").join(sql.Identifier(h) for h in headers),
    )
    for batch in _iter_batches(rows, BATCH_SIZE):
        values = [tuple((r.get(h) or None) for h in headers) for r in batch]
        execute_values(cur, insert_stmt.as_string(cur.connection), values)
        inserted += len(batch)
    return inserted


def main() -> None:
    headers, rows = _read_csv(CSV_PATH)
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_table(cur, headers)
            if TRUNCATE:
                _truncate(cur)
            count = _insert_rows(cur, headers, rows)
        conn.commit()
    print(
        f"Imported rows={count} table={SCHEMA}.{TABLE} csv={CSV_PATH} truncate={TRUNCATE}"
    )


if __name__ == "__main__":
    main()
