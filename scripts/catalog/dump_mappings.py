#!/usr/bin/env python3
"""Regenerate the index-mapping artefacts under docs/specs/ from es_schema.py.

``catalog/es_schema.py`` is authoritative. This writes the JSON copies that the
spec documents reference, so the two can never disagree —
``tests/test_catalog_es_schema.py`` fails if they do.

Usage
-----
  python scripts/catalog/dump_mappings.py            # write
  python scripts/catalog/dump_mappings.py --check    # exit 1 if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.catalog.es_schema import recipe_index, recipe_profile_index

TARGETS = {
    REPO_ROOT / "docs/specs/recipes_v3_mapping.json": recipe_index,
    REPO_ROOT / "docs/specs/recipe_profiles_v1_mapping.json": recipe_profile_index,
}


def render(build) -> str:
    return json.dumps(build(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Verify the artefacts are current instead of writing them.",
    )
    args = ap.parse_args()

    stale: list[Path] = []
    for path, build in TARGETS.items():
        want = render(build)
        have = path.read_text() if path.exists() else None
        if args.check:
            if have != want:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(want)
        print(f"wrote {path.relative_to(REPO_ROOT)}")

    if args.check:
        if stale:
            for path in stale:
                print(f"STALE: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            print("Run: python scripts/catalog/dump_mappings.py", file=sys.stderr)
            raise SystemExit(1)
        print("mapping artefacts are current")


if __name__ == "__main__":
    main()
