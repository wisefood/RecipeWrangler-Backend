"""Deterministic handling of first-listed ingredient alternatives."""

from __future__ import annotations

import re
from dataclasses import dataclass


_OR_RE = re.compile(r"\s+\(?or\s+(?!(?:until|when|as|more|less|to taste)\b)", re.IGNORECASE)
_SHARED_HEADS = frozenset(
    {"cube", "juice", "mushroom", "oil", "pasta", "pepper", "rice", "soup", "steak", "stock"}
)
_COMPLETE_FIRST_WORDS = frozenset({"chive", "passata"})
_TRAILING_FACETS = frozenset(
    {"raw", "cooked", "dried", "frozen", "canned", "tinned", "prepared", "reconstituted"}
)
_COMPLETE_FIRST_HEADS = frozenset(
    {
        "bacon", "beef", "bread", "broth", "butter", "fruit", "herb", "herbs",
        "ketchup", "lamb", "margarine", "milk", "nuts", "paste", "peanuts",
        "pittas", "powder", "prawns", "puree", "raisins", "sauce", "spread",
        "stock", "sultanas", "thyme", "vinegar", "water",
        "fillet", "breast", "thigh", "leg", "flour",
    }
)


@dataclass(frozen=True)
class AlternativeSelection:
    selected: str
    source_context: str
    alternatives: tuple[str, ...]
    policy: str = "first_listed_default"


def select_first_alternative(
    ingredient_name: str,
    source_text: str = "",
    persisted_alternatives: tuple[str, ...] = (),
    persisted_selected: str = "",
) -> AlternativeSelection | None:
    """Select the first branch and restore a shared head noun when necessary."""
    name_parts = _parts(ingredient_name)
    source_parts = _parts(source_text)
    if persisted_selected:
        alternatives = persisted_alternatives or (persisted_selected,)
        context = _source_context(source_parts, persisted_selected)
        return AlternativeSelection(persisted_selected, context, alternatives)

    if len(name_parts) > 1:
        first = re.sub(r"^either\s+", "", name_parts[0], flags=re.IGNORECASE)
        selected = _complete_first_branch(first, name_parts[-1])
        context = _source_context(source_parts, selected)
        return AlternativeSelection(selected, context, tuple(name_parts))
    if len(source_parts) > 1 and not re.search(
        r"\bor\s+(?:until|when|as)\b", source_text, re.IGNORECASE
    ):
        selected = ingredient_name.strip()
        # Only the first source branch may contribute preservation or cooking
        # facets; the canonical parsed name supplies a noun when it appears later.
        context = _source_context(source_parts, selected)
        return AlternativeSelection(selected, context, tuple(source_parts))
    return None


def selected_source_branch(name: str, line: str) -> str | None:
    """Quote a complete first food prefix; never borrow a later option's facts."""
    from recipe_wrangler.matching.occurrence_facts import _head_covers
    choice = select_first_alternative(name, line)
    if choice is None:
        return None
    first = choice.source_context
    if (not first or not line.lstrip().startswith(first) or not _head_covers(first, name)
            or first.count('(') != first.count(')')):
        return None
    suffix = line[line.index(first) + len(first):]
    if suffix.lstrip().startswith('(') and not re.fullmatch(r'\s*\(\s*or\b[^()]*\)\s*[.;]?', suffix, re.I):
        return None
    return first


def _parts(value: str) -> list[str]:
    return [part.strip(" ,.;") for part in _OR_RE.split(value.strip()) if part.strip(" ,.;")]


def _source_context(source_parts: list[str], selected: str) -> str:
    prefix = source_parts[0] if source_parts else ""
    if selected.casefold() in prefix.casefold():
        return prefix
    return " ".join(part for part in (prefix, selected) if part)


def _complete_first_branch(first: str, second: str) -> str:
    first_words = re.findall(r"[a-z]+", first.casefold())
    second_words = re.findall(r"[a-z]+", second.casefold())
    if not first_words or not second_words:
        return first

    # "broth or stock, beef, dehydrated" carries the comma qualifiers to broth.
    if first_words[-1] == "broth" and second_words[0] == "stock":
        suffix = re.sub(r"^stock\b", "", second, count=1, flags=re.IGNORECASE)
        return f"{first}{suffix}"

    head_words = [word for word in second_words if word not in _TRAILING_FACETS]
    if not head_words:
        return first
    shared_head = " ".join(head_words[-2:]) if head_words[-2:] in (["stock", "cube"], ["bell", "pepper"]) else head_words[-1]
    first_head = first_words[-1]
    if first_words in (["beef"], ["lamb"]) and shared_head in {"stock", "soup"}:
        return f"{first} {shared_head}"
    if (
        head_words[-1] not in _SHARED_HEADS
        or head_words[-1] in first_words
        or first_head in _COMPLETE_FIRST_HEADS
        or first_head in _COMPLETE_FIRST_WORDS
        or (first_head.endswith("s") and not first_head.endswith("ss"))
    ):
        return first
    return f"{first} {shared_head}"
