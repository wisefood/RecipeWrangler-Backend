import json
import os
import subprocess
import argparse
from pathlib import Path
from typing import Generator, Iterable

# Purpose: Load recipe-level USDA nutrition JSON into Postgres.

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None

if load_dotenv:
    load_dotenv()

DATA_PATH = Path("data/processed/recipe1m/usda-recipes-nutrition.json")

CONTAINER = os.getenv("POSTGRES_CONTAINER", "wisefood-postgres")
DB_NAME = os.getenv("NUTRITION_DB") or os.getenv("POSTGRES_DB") or "nutrients"
DB_USER = os.getenv("NUTRITION_USER") or os.getenv("POSTGRES_USER") or "postgres"
SCHEMA = os.getenv("NUTRITION_SCHEMA", "public")
TABLE = os.getenv("NUTRITION_RECIPES_TABLE", "nutrients-recipes-usda")

BATCH_SIZE = int(os.getenv("USDA_RECIPES_IMPORT_BATCH_SIZE", "250"))
CHUNK_SIZE = int(os.getenv("USDA_RECIPES_IMPORT_CHUNK_SIZE", "65536"))


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


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
                    # Need more data; compact buffer and read again.
                    buf = buf[idx:]
                    idx = 0
                    break

                yield obj
                idx = next_idx

            # Compact the buffer to keep memory bounded.
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


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_per_serving(
    total_nutrients: dict | None, serves: object
) -> dict[str, float] | None:
    if not isinstance(total_nutrients, dict):
        return None
    serves_f = _to_float(serves)
    if not serves_f or serves_f <= 0:
        return None

    # Only derive for flat numeric nutrient payloads (e.g. MyPlate totals_usda).
    out: dict[str, float] = {}
    for key, value in total_nutrients.items():
        val = _to_float(value)
        if val is None:
            return None
        out[str(key)] = val / serves_f
    return out


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import USDA recipe nutrition JSON to Postgres.")
    parser.add_argument(
        "--data-path",
        type=Path,
        default=DATA_PATH,
        help="Path to nutrition JSON array.",
    )
    parser.add_argument(
        "--default-source",
        type=str,
        default="recipe1m",
        help="Source value if row does not include `source`.",
    )
    parser.add_argument(
        "--truncate-first",
        action="store_true",
        help="Truncate destination table before import (full replace mode).",
    )
    return parser.parse_args()


def run_import(data_path: Path, default_source: str, truncate_first: bool) -> None:
    if not data_path.exists():
        raise FileNotFoundError(f"Missing data file: {data_path}")

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
  recipe_id       text PRIMARY KEY,
  title           text NOT NULL,
  total_nutrients jsonb NOT NULL,
  total_nutrients_per_serving jsonb,
  nutri_score     jsonb,
  source          text
);
ALTER TABLE {table_fq}
ADD COLUMN IF NOT EXISTS total_nutrients_per_serving jsonb;
COMMIT;
"""
    )
    if truncate_first:
        proc.stdin.write(
            f"""
BEGIN;
TRUNCATE TABLE {table_fq};
COMMIT;
"""
        )

    inserted = 0
    skipped = 0

    for batch in batch_iter(iter_json_array(data_path), BATCH_SIZE):
        values: list[str] = []
        for recipe in batch:
            recipe_id = recipe.get("id") or recipe.get("recipe_id")
            title = recipe.get("title")
            total_nutrients = recipe.get("total_nutrients") or recipe.get("totals_usda")
            total_nutrients_per_serving = (
                recipe.get("total_nutrients_per_serving")
                or recipe.get("totals_per_serving_usda")
                or _derive_per_serving(
                    total_nutrients=total_nutrients,
                    serves=recipe.get("serves"),
                )
            )
            nutri_score = recipe.get("nutri_score")
            source = recipe.get("source") or default_source

            if not recipe_id or not title or total_nutrients is None:
                skipped += 1
                continue

            title_sql = sql_escape(str(title))
            total_nutrients_sql = sql_escape(
                json.dumps(total_nutrients, separators=(",", ":"))
            )
            per_serving_sql = (
                f"'{sql_escape(json.dumps(total_nutrients_per_serving, separators=(',', ':')))}'::jsonb"
                if total_nutrients_per_serving is not None
                else "NULL"
            )
            source_sql = sql_escape(str(source))
            nutri_score_sql = (
                f"'{sql_escape(json.dumps(nutri_score, separators=(',', ':')))}'::jsonb"
                if nutri_score is not None
                else "NULL"
            )

            values.append(
                f"('{recipe_id}','{title_sql}','{total_nutrients_sql}'::jsonb,{per_serving_sql},{nutri_score_sql},'{source_sql}')"
            )

        if not values:
            continue

        values_sql = ",\n".join(values)
        proc.stdin.write(
            f"""
BEGIN;
INSERT INTO {table_fq} (
  recipe_id, title, total_nutrients, total_nutrients_per_serving, nutri_score, source
)
VALUES
{values_sql}
ON CONFLICT (recipe_id) DO UPDATE SET
  title = EXCLUDED.title,
  total_nutrients = EXCLUDED.total_nutrients,
  total_nutrients_per_serving = EXCLUDED.total_nutrients_per_serving,
  nutri_score = EXCLUDED.nutri_score,
  source = EXCLUDED.source;
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
    args = _parse_args()
    run_import(
        data_path=args.data_path,
        default_source=args.default_source,
        truncate_first=bool(args.truncate_first),
    )
