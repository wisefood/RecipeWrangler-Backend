import argparse
import ast
import csv
import json
from pathlib import Path


# Purpose: Enrich recipe1m-ex-limited JSON with HUMMUS metadata by normalized URL.

DEFAULT_RECIPE1M = "./data/processed/recipe1m/recipe1m-ex-limited.json"
DEFAULT_HUMMUS = "./data/processed/hummus/pp_recipes.csv"
DEFAULT_OUTPUT = "./data/processed/recipe1m/recipe1m-ex-limited-hummus-metadata.json"


def norm_url(url: str) -> str:
    url = (url or "").strip().lower()
    url = url.replace("https://", "http://")
    url = url.replace("http://food.com/", "http://www.food.com/")
    if url.endswith("/"):
        url = url[:-1]
    return url


def parse_tags(value: str):
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, SyntaxError):
            return [value]
    return [value]


def load_hummus_metadata(path: Path) -> dict[str, dict]:
    metadata = {}
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = norm_url(row.get("recipe_url"))
            if not url:
                continue
            metadata[url] = {
                "tags": parse_tags(row.get("tags")),
                "duration": row.get("duration"),
                "serves": row.get("serves"),
                "who_score": row.get("who_score"),
                "fsa_score": row.get("fsa_score"),
                "nutri_score": row.get("nutri_score"),
            }
    return metadata


def stream_recipe1m(path: Path):
    decoder = json.JSONDecoder()
    buffer = ""
    in_array = False

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            buffer += chunk
            while True:
                if not in_array:
                    stripped = buffer.lstrip()
                    if not stripped:
                        break
                    if stripped[0] == "[":
                        buffer = stripped[1:]
                        in_array = True
                    else:
                        raise ValueError("Expected JSON array in recipe1m file.")

                buffer = buffer.lstrip()
                if not buffer:
                    break
                if buffer[0] == "]":
                    return
                if buffer[0] == ",":
                    buffer = buffer[1:]
                    continue

                try:
                    obj, idx = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    break
                buffer = buffer[idx:]
                yield obj

        buffer = buffer.lstrip()
        if buffer and buffer[0] != "]":
            raise ValueError("Incomplete JSON array in recipe1m file.")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich recipe1m-ex-limited with selected HUMMUS metadata."
    )
    parser.add_argument(
        "--recipe1m",
        default=DEFAULT_RECIPE1M,
        help=f"Path to recipe1m-ex-limited.json (default: {DEFAULT_RECIPE1M})",
    )
    parser.add_argument(
        "--hummus",
        default=DEFAULT_HUMMUS,
        help=f"Path to HUMMUS pp_recipes.csv (default: {DEFAULT_HUMMUS})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write enriched JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    recipe1m_path = Path(args.recipe1m)
    hummus_path = Path(args.hummus)
    output_path = Path(args.output)

    hummus_meta = load_hummus_metadata(hummus_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        out.write("[")
        first = True
        matched = 0
        total = 0

        for recipe in stream_recipe1m(recipe1m_path):
            total += 1
            url = norm_url(recipe.get("url"))
            meta = hummus_meta.get(url)
            if meta:
                recipe.update(meta)
                matched += 1
            if not first:
                out.write(",\n")
            else:
                first = False
            out.write(json.dumps(recipe, ensure_ascii=True))

        out.write("]\n")

    print(f"Total recipes: {total}")
    print(f"Enriched with HUMMUS metadata: {matched}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
