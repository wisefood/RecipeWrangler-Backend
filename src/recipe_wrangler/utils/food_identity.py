"""Shared deterministic food-identity comparison; retrieval rank is not equivalence."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


IdentityRelation = Literal[
    "exact",
    "synonym",
    "likely_equivalent",
    "different",
    "insufficient_information",
]

_SYNONYMS = {
    "peppercorn": "pepper",
    "aubergine": "eggplant",
    "broth": "stock",
    "canola": "rapeseed",
    "cilantro": "coriander",
    "corn": "maize",
    "courgette": "zucchini",
    "garbanzo": "chickpea",
    "prawn": "shrimp",
    "rocket": "arugula",
    "yoghourt": "yogurt",
    "yoghurt": "yogurt",
    "wrap": "tortilla",
    "calamari": "squid",
}
_IRREGULAR = {
    "chickpeas": "chickpea",
    "children": "child",
    "cloves": "clove",
    "leaves": "leaf",
    "loaves": "loaf",
    "potatoes": "potato",
    "tomatoes": "tomato",
}
_FACET_WORDS = frozenset(
    {
        "scrambled", "skinless", "boneless", "baked", "blanched", "boiled", "braised", "canned", "chopped",
        "concentrated", "cooked", "crushed", "cured", "dehydrated", "diced",
        "dried", "drained", "dry", "fermented", "flakes", "frozen", "fried",
        "grated", "grilled", "ground",
        "minced", "peeled", "pickled", "powder", "powdered", "raw",
        "reduced", "refined", "rinsed", "roast", "roasted", "salted",
        "sauteed", "sautéed", "slice", "sliced", "slices", "smoked", "steamed", "tinned",
        "toasted", "unsalted", "whole", "wholegrain", "wholemeal", "poached",
        "black", "blue", "brown", "green", "orange", "purple", "red",
        "white", "yellow",
    }
)
_LABEL_WORDS = frozenset(
    {
        "added", "average", "commercial", "contents", "extra", "fresh", "grain", "in",
        "natural", "no", "of", "prepared", "retail", "style", "the", "type",
        "virgin", "with", "without", "wo",
    }
)
_IDENTITY_DESCRIPTORS = frozenset(
    {
        # Controlled variety/origin qualifiers. These may make a candidate
        # more specific without introducing another food identity.
        "arborio",
        "british",
        "english",
        "french",
        "hungarian",
        "irish",
        "italian",
        "slovenian",
    }
)


@dataclass(frozen=True)
class IdentityEvidence:
    relation: IdentityRelation
    mention_key: tuple[str, ...]
    candidate_key: tuple[str, ...]
    shared_tokens: tuple[str, ...]
    reasons: tuple[str, ...]


def compare_identities(mention: str | None, candidate: str | None) -> IdentityEvidence:
    """Compare labels lexically without using facets, source, or retrieval rank."""
    if not mention or not candidate:
        return IdentityEvidence(
            "insufficient_information", (), (), (), ("missing_identity",)
        )

    mention_normalized = _normalized_text(mention)
    candidate_normalized = _normalized_text(candidate)
    if mention_normalized == candidate_normalized:
        key, _ = _identity_key(mention_normalized)
        return IdentityEvidence("exact", key, key, key, ("normalized_text_equal",))

    mention_key, mention_synonym = _identity_key(mention_normalized)
    candidate_key, candidate_synonym = _identity_key(candidate_normalized)
    shared = tuple(sorted(set(mention_key) & set(candidate_key)))
    if not mention_key or not candidate_key:
        relation: IdentityRelation = "insufficient_information"
        reasons = ("identity_empty_after_qualifier_removal",)
    elif mention_key == candidate_key:
        relation = "synonym" if mention_synonym or candidate_synonym else "likely_equivalent"
        reasons = ("canonical_identity_tokens_equal",)
    elif (
        len(mention_key) >= 2
        and set(mention_key).issubset(candidate_key)
        and set(candidate_key).difference(mention_key).issubset(_IDENTITY_DESCRIPTORS)
    ):
        # Retrieval labels may add a controlled variety or origin around the
        # complete multi-token identity. Arbitrary extra food words remain
        # unsafe (chicken breast/ham, rice milk/rice pudding).
        relation = "likely_equivalent"
        reasons = ("complete_mention_token_containment",)
    elif not shared:
        relation = "different"
        reasons = ("no_shared_identity_tokens",)
    else:
        relation = "insufficient_information"
        reasons = ("partial_identity_token_overlap",)
    return IdentityEvidence(relation, mention_key, candidate_key, shared, reasons)


def _normalized_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    # Source record codes are noise only when explicitly separated from the label.
    folded = re.sub(r"^\s*[a-z]*\d+(?:[._-]\d+)*\s*[-:]\s*", "", folded, flags=re.I)
    folded = re.sub(
        r"\b(?:low[- ]fat|half[- ]fat|reduced[- ]fat|\d+(?:\.\d+)?\s*%\s+less\s+fat|"
        r"no[- ]added[- ]salt|without[- ]added[- ]salt|wo\s+salt|"
        r"no[- ]added[- ]sugar)\b",
        " ",
        folded,
        flags=re.I,
    )
    folded = re.sub(r"\b\d+(?:\.\d+)?\s*%", " ", folded)
    folded = re.sub(r"\b(?:with|without|and)\s+skin\b|\bbone[- ]in\b|\bin\s+(?:spring\s+)?(?:oil|brine|water)\b", " ", folded, flags=re.I)
    folded = re.sub(r"\bchick\s+peas?\b", "chickpea", folded.casefold())
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def _identity_key(value: str) -> tuple[tuple[str, ...], bool]:
    used_synonym = False
    tokens: set[str] = set()
    for raw in value.split():
        if raw in _FACET_WORDS or raw in _LABEL_WORDS:
            continue
        token = _IRREGULAR.get(raw, raw)
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif token.endswith("s") and not token.endswith(("ss", "us")) and len(token) > 3:
            token = token[:-1]
        canonical = _SYNONYMS.get(token, token)
        used_synonym |= canonical != token
        tokens.add(canonical)
    return tuple(sorted(tokens)), used_synonym
