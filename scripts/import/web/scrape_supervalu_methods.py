#!/usr/bin/env python3
"""Backfill missing SuperValu methods from their official recipe pages.

The normalized SuperValu export contains some records whose
``extra_fields.method_steps`` is empty even though the linked page has a
Method section.  This script reads only the page-authored
``itemprop=recipeInstructions`` content and updates those empty records.

Dry-run by default. Use ``--write`` only after every targeted page succeeds.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = REPO_ROOT / "data" / "SuperValu" / "supervalu.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
_STEP_NUMBER_RE = re.compile(r"^\s*\d+\s*[.)]\s*")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_method_html(html: str) -> list[str]:
    """Return exact displayed blocks from SuperValu's Method section."""

    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one('[itemprop="recipeInstructions"]')
    if container is None:
        raise ValueError("recipeInstructions section not found")

    blocks = container.find_all(["p", "li"])
    if blocks:
        raw_steps = [_clean(block.get_text(" ", strip=True)) for block in blocks]
    else:
        raw_steps = [
            _clean(line)
            for line in container.get_text("\n", strip=True).splitlines()
        ]

    steps = []
    for raw_step in raw_steps:
        step = _STEP_NUMBER_RE.sub("", raw_step)
        if step:
            steps.append(step)
    if not steps:
        raise ValueError("recipeInstructions section is empty")
    return steps


def fetch_method(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
) -> list[str]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return parse_method_html(response.text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--all", action="store_true", help="re-fetch non-empty methods too")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    recipes = json.loads(args.input.read_text(encoding="utf-8"))
    targets = [
        recipe
        for recipe in recipes
        if args.all or not ((recipe.get("extra_fields") or {}).get("method_steps") or [])
    ]
    print(f"[supervalu] {len(targets)} recipes to fetch")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    fetched: dict[str, list[str]] = {}
    failures: list[tuple[str, str]] = []
    for index, recipe in enumerate(targets, 1):
        title = str(recipe.get("title") or recipe.get("id"))
        url = str(recipe.get("url") or "").strip()
        if not url:
            failures.append((title, "missing URL"))
            continue
        try:
            steps = fetch_method(session, url, timeout=args.timeout)
            fetched[str(recipe["id"])] = steps
            print(f"[{index}/{len(targets)}] {title}: {len(steps)} steps")
        except Exception as exc:  # one page should not hide the rest of the audit
            failures.append((title, str(exc)))
            print(f"[{index}/{len(targets)}] {title}: ERROR {exc}")
        if args.delay:
            time.sleep(args.delay)

    if failures:
        print(f"[supervalu] {len(failures)} failures; source file unchanged")
        for title, error in failures:
            print(f"  {title}: {error}")
        raise SystemExit(1)

    if args.write:
        for recipe in recipes:
            steps = fetched.get(str(recipe.get("id")))
            if steps is not None:
                recipe.setdefault("extra_fields", {})["method_steps"] = steps
        temporary = args.input.with_suffix(args.input.suffix + ".tmp")
        temporary.write_text(
            json.dumps(recipes, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.input)
        print(f"[supervalu] wrote {len(fetched)} methods to {args.input}")
    else:
        print(f"[supervalu] dry-run complete: {len(fetched)} methods; use --write to save")


if __name__ == "__main__":
    main()
