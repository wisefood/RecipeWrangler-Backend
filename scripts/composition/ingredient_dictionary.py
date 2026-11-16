#!/usr/bin/env python3
"""Build or inspect a local ingredient dictionary without external services."""

import argparse
import json
from pathlib import Path

from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary, build_dictionary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-dir", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--dictionary", type=Path, required=True)
    extract.add_argument("--text", required=True)
    args = parser.parse_args()
    if args.command == "build":
        dictionary = build_dictionary(args.source_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to replace a frozen dictionary accidentally.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(dictionary.payload, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"concepts": len(dictionary.payload["terms"]), "normalized_labels": len(dictionary.index), "ambiguous_labels": sum(len({h.term_id for h in hits}) > 1 for hits in dictionary.index.values()), "output": str(args.output)}))
    else:
        result = IngredientDictionary.load(args.dictionary).extract(args.text)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=lambda value: sorted(value) if isinstance(value, frozenset) else str(value)))


if __name__ == "__main__":
    main()
