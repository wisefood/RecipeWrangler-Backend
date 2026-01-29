import argparse
import json
from pathlib import Path
from typing import Any, List

from tqdm import tqdm

from recipe_wrangler.utils.get_embeddings import get_embeddings_batch


def _batched(items: List[Any], batch_size: int) -> List[List[Any]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def embed_file(input_path: Path, output_path: Path, batch_size: int) -> None:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Input JSON must be a list of objects.")

    names = [str(row.get("canonical", "")).strip() for row in data]
    embeddings: List[List[float]] = []

    for batch in tqdm(_batched(names, batch_size), desc="Embedding canonicals", unit="batch"):
        batch_embs = get_embeddings_batch(batch)
        embeddings.extend(batch_embs)

    if len(embeddings) != len(data):
        raise ValueError("Embedding count mismatch.")

    for row, emb in zip(data, embeddings):
        row["embedding"] = [float(x) for x in emb]

    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed canonical ingredients and write JSON with embeddings.")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/processed/recipe1m/recipe1m_canonical_id_name.json"),
        help="Input JSON path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/recipe1m/recipe1m_canonical_id_name_embeddings.json"),
        help="Output JSON path.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Embedding batch size.",
    )
    args = parser.parse_args()

    embed_file(args.input, args.output, args.batch_size)


if __name__ == "__main__":
    main()
