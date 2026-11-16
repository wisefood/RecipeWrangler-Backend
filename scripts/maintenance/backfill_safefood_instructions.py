"""Recover Safefood method steps accidentally captured in ``equipment``.

The legacy scraper selected the whole recipe-details container for equipment,
so those arrays contain ingredients, the complete method, then actual tools.
This command is dry-run by default and atomically updates JSON with ``--write``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


DEFAULT_INPUTS = sorted(Path("data/SafeFood_web").glob("safefood_*_recipes.json"))
_ACTION_RE = re.compile(
    r"\b(?:add|allow|arrange|bake|beat|blend|boil|bring|chop|combine|cook|cover|"
    r"cut|dice|drain|fill|fold|form|fry|heat|knead|make|melt|mix|peel|place|"
    r"pour|preheat|pre-heat|reduce|refrigerate|remove|return|rinse|roll|season|"
    r"serve|sift|simmer|sprinkle|stir|top|transfer|turn|whisk)\b",
    re.IGNORECASE,
)


def recovered_method(recipe: dict) -> list[str] | None:
    """Return a longer source-authored method when the legacy copy proves it."""

    method = recipe.get("method") or []
    captured = recipe.get("equipment") or []
    if not method or not captured:
        return None
    starts = [
        index
        for index in range(len(captured) - len(method) + 1)
        if captured[index : index + len(method)] == method
    ]
    if not starts:
        return None

    recovered = list(method)
    for value in captured[starts[-1] + len(method) :]:
        text = str(value).strip()
        if len(text.split()) < 6 and not _ACTION_RE.search(text):
            break
        recovered.append(text)
    return recovered if len(recovered) > len(method) else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("inputs", nargs="*", type=Path, default=DEFAULT_INPUTS)
    args = parser.parse_args()

    total = 0
    for path in args.inputs:
        recipes = json.loads(path.read_text(encoding="utf-8"))
        changed = []
        for recipe in recipes:
            method = recovered_method(recipe)
            if method:
                recipe["method"] = method
                changed.append((recipe.get("name"), len(method)))
        total += len(changed)
        print(f"{path}: recoverable={len(changed)}")
        for title, steps in changed:
            print(f"  {title}: {steps} steps")
        if args.write and changed:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
    print(f"recoverable_truncated_methods={total}")


if __name__ == "__main__":
    main()
