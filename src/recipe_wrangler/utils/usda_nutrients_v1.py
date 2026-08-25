# Purpose: USDA identifier links used only by portion-weight estimation.

import json
import os
from functools import lru_cache
import re
from pathlib import Path
from typing import Optional

DEFAULT_LINKS = Path(os.getenv("USDA_LINKS_PATH", ":pg:usda-portion-links"))


def _load_data(path: str) -> list:
    """Load from local file or Postgres depending on path sentinel."""
    if path.startswith(":pg:"):
        from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data

        try:
            return load_pipeline_data(path[4:])
        except Exception:
            # Static enrichment is optional. Weight parsing and deterministic
            # fallbacks must still work while the nutrition DB is unavailable.
            return []
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


@lru_cache(maxsize=1)
def _canonical_links(links_path: str) -> dict[str, dict]:
    data = _load_data(links_path)
    return {
        str(row["canonical_id"]): row
        for row in data
        if row.get("canonical_id")
    }


def canonical_to_usda(
    canonical_id: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    return _canonical_links(str(links_path)).get(str(canonical_id))


def canonical_name_to_usda(
    canonical_name: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    canonical_lower = _normalize_canonical_name(str(canonical_name))
    for row in _canonical_links(str(links_path)).values():
        if _normalize_canonical_name(str(row.get("canonical", ""))) == canonical_lower:
            return row
    return None


def _normalize_canonical_name(name: str) -> str:
    cleaned = str(name).strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    tokens = cleaned.split()
    drop_tokens = {
        "fresh",
        "ground",
        "minced",
        "chopped",
        "large",
        "small",
        "medium",
        "to",
        "taste",
    }
    countable_suffixes = {
        "cloves": "clove",
        "clove": "clove",
        "sprigs": "sprig",
        "sprig": "sprig",
        "leaves": "leaf",
        "leaf": "leaf",
        "stalks": "stalk",
        "stalk": "stalk",
        "sticks": "stick",
        "stick": "stick",
        "slices": "slice",
        "slice": "slice",
        "pieces": "piece",
        "piece": "piece",
        "bunches": "bunch",
        "bunch": "bunch",
    }
    normalized = []
    for token in tokens:
        if token in drop_tokens:
            continue
        normalized.append(countable_suffixes.get(token, token))
    return " ".join(normalized).strip()


def usda_id_to_link(
    usda_id: str,
    links_path: Path = DEFAULT_LINKS,
) -> Optional[dict]:
    usda_id_str = str(usda_id)
    for row in _canonical_links(str(links_path)).values():
        if str(row.get("usda_id")) == usda_id_str:
            return row
    return None
