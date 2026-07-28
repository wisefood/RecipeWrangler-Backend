"""Validation helpers for converting branded foods to generic ingredients."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


BRAND_CLASSIFICATION_VERSION = "brand-normalization-v1"

_WS_RE = re.compile(r"\s+")
_ALPHA_RE = re.compile(r"[a-zA-Z]")
_LEADING_QUANTITY_RE = re.compile(
    r"^\s*(?:\d+(?:[./]\d+)?|[¼½¾⅓⅔⅛⅜⅝⅞])(?:\s+|$)"
)
_EMPTY_GENERIC_TERMS = {
    "",
    "brand",
    "branded product",
    "food",
    "ingredient",
    "product",
    "unknown",
}
_VAGUE_SINGLE_TOKEN_GENERICS = {
    "beverage",
    "bar",
    "candy",
    "cereal",
    "cheese",
    "chip",
    "chocolate",
    "cookie",
    "cracker",
    "drink",
    "dressing",
    "filling",
    "gelatin",
    "liqueur",
    "liquor",
    "mix",
    "oil",
    "product",
    "sauce",
    "seasoning",
    "spread",
    "syrup",
    "sweetener",
    "topping",
}
_VAGUE_GENERIC_PHRASES = {
    "seasoning mix",
}
_NON_IDENTITY_TOKENS = {
    "a",
    "an",
    "and",
    "bag",
    "can",
    "for",
    "in",
    "of",
    "or",
    "pack",
    "packet",
    "sachet",
    "the",
    "to",
    "tub",
    "with",
    "x",
}
_SOURCE_LABELS = {
    "curated irish recipes",
    "foodhero",
    "healthyfoods",
    "irish safefood",
    "irish_safefood",
    "myplate",
    "recipe1m",
}
_LOST_IDENTITY_REASON_RE = re.compile(
    r"(?:"
    r"(?:flavou?r|qualifier|specificity|identity).{0,30}(?:is |are )?lost"
    r"|(?:loses?|lost).{0,30}(?:flavou?r|qualifier|specificity|identity)"
    r"|does not preserve"
    r"|fails to preserve"
    r"|should be retained"
    r"|too (?:broad|generic|vague)"
    r"|more specific(?:ity)?"
    r"|more precise"
    r"|needs? (?:manual )?review"
    r"|not sufficiently (?:clear|specific)"
    r"|except for"
    r")",
    re.IGNORECASE,
)
_IDENTITY_QUALIFIER_RES = (
    re.compile(r"\b\d+(?:\.\d+)?\s*%"),
    re.compile(
        r"\b(?:fat|sugar|gluten|dairy|lactose|alcohol)[ -]?free\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:low|reduced)[ -](?:fat|sodium|salt|sugar|calorie|carb)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:light|lite|nonfat|skim|part[ -]skim)\b", re.IGNORECASE),
)


class BrandIngredientDecision(BaseModel):
    """One LLM classification for one currently stored ingredient name."""

    ingredient_name: str = Field(min_length=1)
    is_branded: bool
    brand_name: str | None = None
    generic_name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: Literal["keep", "normalize", "review"]
    reason: str = ""


class BrandIngredientBatch(BaseModel):
    """Structured response for a batch of ingredient classifications."""

    decisions: list[BrandIngredientDecision]


class BrandReviewDecision(BaseModel):
    """Independent review of one first-pass brand candidate."""

    ingredient_name: str = Field(min_length=1)
    verdict: Literal["remove_brand", "keep_original", "needs_review"]
    brand_name: str | None = None
    generic_name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class BrandReviewBatch(BaseModel):
    """Structured response for independent brand-candidate review."""

    decisions: list[BrandReviewDecision]


def clean_generic_name(value: object) -> str:
    """Return a graph-safe generic ingredient label without changing identity."""
    name = str(value or "").strip().lower()
    name = name.replace("®", " ").replace("™", " ")
    name = _WS_RE.sub(" ", name)
    return name.strip(" \t\r\n,;:|")


def _brand_tokens(value: object) -> set[str]:
    normalized = str(value or "").lower().replace("’", "'")
    normalized = normalized.replace("'s", "")
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 1
    }


def generic_name_is_valid(
    original_name: object,
    generic_name: object,
    brand_name: object = None,
) -> bool:
    """Reject empty, unchanged, quantified, or still-branded generic labels."""
    original = clean_generic_name(original_name)
    generic = clean_generic_name(generic_name)
    generic_tokens = _brand_tokens(generic)
    vague_single = (
        generic in _VAGUE_SINGLE_TOKEN_GENERICS
        or (
            generic.endswith("s")
            and generic[:-1] in _VAGUE_SINGLE_TOKEN_GENERICS
        )
        or (
            generic.endswith("ies")
            and f"{generic[:-3]}y" in _VAGUE_SINGLE_TOKEN_GENERICS
        )
    )
    if (
        generic in _EMPTY_GENERIC_TERMS
        or generic == original
        or len(generic) > 180
        or not _ALPHA_RE.search(generic)
        or _LEADING_QUANTITY_RE.match(generic)
        or vague_single
        or generic in _VAGUE_GENERIC_PHRASES
        or not generic_tokens
        or max(map(len, generic_tokens)) < 3
    ):
        return False

    brand_tokens = _brand_tokens(brand_name)
    if brand_tokens and brand_tokens <= generic_tokens:
        return False
    return True


def _token_variants(token: str) -> set[str]:
    variants = {token}
    if len(token) > 4 and token.endswith("ies"):
        variants.add(f"{token[:-3]}y")
    if len(token) > 4 and token.endswith("es"):
        variants.add(token[:-2])
    if len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    if len(token) > 5 and token.endswith(("ed", "er")):
        variants.add(token[:-2])
    if len(token) > 6 and token.endswith(("ated", "ular")):
        variants.add(token[:5])
    return variants


def _unpreserved_original_tokens(
    original_name: str,
    brand_name: str | None,
    generic_name: str | None,
) -> list[str]:
    """Find visible non-brand words lost by the proposed normalization."""
    original_tokens = _brand_tokens(original_name)
    brand_tokens = _brand_tokens(brand_name)
    generic_tokens = _brand_tokens(generic_name)
    brand_variants = set().union(*(_token_variants(t) for t in brand_tokens)) \
        if brand_tokens else set()
    generic_variants = set().union(*(_token_variants(t) for t in generic_tokens)) \
        if generic_tokens else set()

    missing: list[str] = []
    for token in sorted(original_tokens):
        variants = _token_variants(token)
        if (
            token in _NON_IDENTITY_TOKENS
            or variants & brand_variants
            or variants & generic_variants
        ):
            continue
        missing.append(token)
    return missing


def validate_brand_decision(
    expected_name: str,
    decision: BrandIngredientDecision,
) -> BrandIngredientDecision:
    """Normalize an LLM decision and downgrade unsafe normalizations to review."""
    expected = str(expected_name).strip()
    decision.ingredient_name = expected
    decision.brand_name = (
        str(decision.brand_name).strip() if decision.brand_name else None
    )
    decision.generic_name = (
        clean_generic_name(decision.generic_name)
        if decision.generic_name
        else None
    )

    if not decision.is_branded:
        decision.brand_name = None
        decision.generic_name = None
        decision.recommended_action = "keep"
        return decision

    if decision.recommended_action == "normalize" and not generic_name_is_valid(
        expected,
        decision.generic_name,
        decision.brand_name,
    ):
        decision.recommended_action = "review"
        decision.reason = (
            f"{decision.reason}; unsafe or missing generic name".strip("; ")
        )
    return decision


def validate_brand_review_decision(
    expected_name: str,
    decision: BrandReviewDecision,
) -> BrandReviewDecision:
    """Downgrade internally inconsistent or identity-losing review approvals."""
    expected = str(expected_name).strip()
    decision.ingredient_name = expected
    decision.brand_name = (
        str(decision.brand_name).strip() if decision.brand_name else None
    )
    decision.generic_name = (
        clean_generic_name(decision.generic_name)
        if decision.generic_name
        else None
    )
    if decision.verdict != "remove_brand":
        return decision

    brand_key = clean_generic_name(decision.brand_name).replace("-", " ")
    if brand_key in _SOURCE_LABELS:
        decision.verdict = "keep_original"
        decision.reason = (
            f"{decision.reason}; proposed brand is a dataset/source label"
        ).strip("; ")
        return decision

    safety_reasons: list[str] = []
    if not generic_name_is_valid(
        expected,
        decision.generic_name,
        decision.brand_name,
    ):
        safety_reasons.append("unsafe generic name")

    if _LOST_IDENTITY_REASON_RE.search(decision.reason):
        safety_reasons.append("review explanation reports lost identity")

    original = clean_generic_name(expected).replace("-", " ")
    generic = clean_generic_name(decision.generic_name).replace("-", " ")
    missing_tokens = _unpreserved_original_tokens(
        expected,
        decision.brand_name,
        decision.generic_name,
    )
    if missing_tokens:
        safety_reasons.append(
            f"unpreserved original words: {', '.join(missing_tokens)}"
        )
    for pattern in _IDENTITY_QUALIFIER_RES:
        for match in pattern.finditer(original):
            qualifier = _WS_RE.sub(" ", match.group(0).replace("-", " ")).strip()
            if qualifier not in generic:
                safety_reasons.append(f"lost qualifier: {match.group(0)}")

    if safety_reasons:
        decision.verdict = "needs_review"
        decision.reason = (
            f"{decision.reason}; {'; '.join(dict.fromkeys(safety_reasons))}"
        ).strip("; ")
    return decision
