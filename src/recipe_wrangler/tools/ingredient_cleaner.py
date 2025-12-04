"""Lightweight ingredient cleaning tool."""

from __future__ import annotations

import re
from typing import Iterable, List

from langchain.tools import tool


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\\s\\-]", " ", name or "")
    cleaned = re.sub(r"\\s+", " ", cleaned).strip().lower()
    return cleaned


@tool
def ingredient_cleaning_tool(ingredient_names: Iterable[str]) -> dict:
    """
    Normalize a list of ingredient names by lowercasing, stripping punctuation, and collapsing whitespace.
    Returns both cleaned and original values to allow auditing.
    """

    cleaned: List[str] = []
    for raw in ingredient_names or []:
        normalized = _clean_name(str(raw))
        if normalized:
            cleaned.append(normalized)

    return {
        "cleaned": cleaned,
        "original": list(ingredient_names or []),
    }
