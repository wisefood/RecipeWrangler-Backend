"""Allergen evidence read from original ingredient lines, added to name-based evidence.

Stored and parsed names are reduced ("soy milk" -> "milk"), so a definite allergen can be lost.
Lines only ADD evidence the names lack; they never replace or remove it.
"""
from __future__ import annotations

from typing import Any


def is_heading(line: str) -> bool:
    """'Pastry', 'Noodle salad': a capitalised label with no amount names a section, not a food."""
    text = line.strip()
    return bool(text) and text[0].isupper() and not any(c.isdigit() for c in text) \
        and "," not in text and len(text.split()) <= 3


def extra_line_evidence(have: set[str], lines: list[str] | None) -> list[dict[str, Any]]:
    """Definite-allergen evidence rows from lines, for allergens not already in ``have``."""
    from recipe_wrangler.repositories.neo4j_recipes import detect_allergen_evidence_from_names

    extra: list[dict[str, Any]] = []
    seen = set(have)
    for line in lines or []:
        text = str(line or "").strip()
        if not text or is_heading(text):
            continue
        for row in detect_allergen_evidence_from_names([text]):
            if row["presence"] == "contains" and row["allergen"] not in seen:
                seen.add(row["allergen"])
                extra.append(row)
    return extra
