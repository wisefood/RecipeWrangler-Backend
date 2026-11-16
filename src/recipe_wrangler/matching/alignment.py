"""Validation for parsed ingredients paired with their source text."""

from __future__ import annotations

import re
from collections.abc import Sequence


_STOPWORDS = frozenset(
    {
        "and", "can", "cup", "dessertspoon", "fresh", "gram", "grams",
        "large", "medium", "millilitre", "ounce", "ounces", "small",
        "tablespoon", "teaspoon", "the", "tin", "with",
    }
)


def source_alignment_mismatch(original_text: str, ingredient_name: str) -> bool:
    """Return true only when both texts exist and share no food-identity token."""
    original = _tokens(original_text)
    parsed = _tokens(ingredient_name)
    if not (original and parsed) or original.intersection(parsed):
        return False
    # Keep harmless token-boundary changes such as blackberries/black berries.
    return "".join(_token_list(original_text)) != "".join(_token_list(ingredient_name))


def align_source_texts(
    ingredient_names: Sequence[str],
    original_texts: Sequence[str],
) -> list[str]:
    """Pair source lines to parsed ingredients by identity, without reuse."""
    available = set(range(len(original_texts)))
    aligned = [""] * len(ingredient_names)
    edges: list[tuple[int, int, int, int]] = []
    for ingredient_index, name in enumerate(ingredient_names):
        parsed = _tokens(name)
        for original_index, text in enumerate(original_texts):
            shared = parsed.intersection(_tokens(text))
            boundary_match = "".join(_token_list(name)) in "".join(_token_list(text))
            if shared or boundary_match:
                edges.append(
                    (
                        len(shared) + int(boundary_match),
                        -abs(ingredient_index - original_index),
                        ingredient_index,
                        original_index,
                    )
                )
    assigned: set[int] = set()
    for _, _, ingredient_index, original_index in sorted(edges, reverse=True):
        if ingredient_index not in assigned and original_index in available:
            aligned[ingredient_index] = original_texts[original_index]
            assigned.add(ingredient_index)
            available.remove(original_index)
    return aligned


def _tokens(value: str) -> set[str]:
    return set(_token_list(value))


def _token_list(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z]+", value.casefold())
        if len(token) > 2 and token not in _STOPWORDS
    ]
