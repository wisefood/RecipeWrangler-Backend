"""Shared, evidence-preserving queries for experimental candidate retrieval."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from recipe_wrangler.matching.facets import extract_candidate_facets
from recipe_wrangler.matching.models import FoodMention, FoodCandidate, FoodFacets, RetrievalEvidence

if TYPE_CHECKING:
    from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue


_HANDLING = re.compile(
    r"\b(?:finely|roughly|coarsely|thinly|chopped|diced|sliced|grated|"
    r"peeled|rinsed|trimmed|optional|divided)\b", re.IGNORECASE
)
_AMOUNT = re.compile(
    r"^(?!\s*\d+(?:[.,]\d+)?\s*%)\s*(?:\d[\d\s./¼½¾⅓⅔⅛⅜⅝⅞-]*|[¼½¾⅓⅔⅛⅜⅝⅞])\s*"
    r"(?:(?:kilograms?|grams?|kg|g|millilitres?|ml|litres?|l|cups?|"
    r"tablespoons?|teaspoons?|tbsp|tsp|ounces?|oz|pounds?|lbs?)\b)?\s*",
    re.IGNORECASE,
)


def _phrase(value: str) -> str:
    value = _AMOUNT.sub("", value)
    value = _HANDLING.sub(" ", value)
    return " ".join(re.sub(r"[,;()]", " ", value).casefold().split())


def retrieval_queries(mention: FoodMention) -> tuple[str, ...]:
    """Search the qualified phrase first, then the parser's identity.

    Facets recovered from source notes are included in the qualified search.
    The identity search broadens recall only; selection still uses all facets.
    """
    identity = _phrase(mention.facets.identity or mention.identity)
    qualified = _phrase(mention.text) or identity
    facets = mention.facets
    stated = extract_candidate_facets(qualified).facets
    qualifiers: list[str] = []
    for field in (
        "biological_source", "food_part", "preparation_state", "physical_form", "colour", "grain_refinement",
    ):
        value = getattr(facets, field)
        if value and value != "unknown" and getattr(stated, field) != value:
            qualifiers.append(value.replace("_", " "))
    for field, suffix in (("sodium_level", "salt"), ("fat_level", "fat"), ("sugar_level", "sugar")):
        value = getattr(facets, field)
        if value and value != "unknown":
            wording = value.replace("_", " ")
            if field == "sodium_level" and value in {"low", "reduced", "regular"}:
                wording += " salt"
            elif field != "sodium_level" and value in {"low", "reduced"}:
                wording += f" {suffix}"
            qualifiers.append(wording)
    for field, absent, present in (("skin_status", "skinless", "with skin"), ("bone_status", "boneless", "bone in")):
        value = getattr(facets, field)
        if value in {"absent", "present"} and getattr(stated, field) != value:
            qualifiers.append(absent if value == "absent" else present)
    if facets.packing_medium and facets.packing_medium != stated.packing_medium:
        qualifiers.append(f"in {facets.packing_medium}")
    if facets.identity and facets.identity.casefold() != mention.identity.casefold():
        qualified = identity
    if facets.gluten_free is True:
        qualifiers.append("gluten free")
    qualifiers.extend(sorted(facets.preservation_methods))
    qualifiers.extend(
        process for process in sorted(facets.processes)
        if not _HANDLING.fullmatch(process)
    )
    for qualifier in qualifiers:
        # Compare words across hyphenation without discarding the source wording.
        if not re.search(r"\b" + re.escape(qualifier) + r"\b", qualified.replace("-", " ")):
            qualified = f"{qualified} {qualifier}".strip()
    return tuple(dict.fromkeys(query for query in (qualified, identity) if query))


def retrieve_candidates(
    mention: FoodMention,
    retrieve: Callable[[str], list[dict]],
    record_id: Callable[[dict], str],
    limit: int = 25,
) -> list[dict]:
    """Fuse query rankings by reciprocal rank, preserving contributing queries."""
    if limit < 1:
        return []
    merged: dict[str, dict] = {}
    for query in retrieval_queries(mention):
        seen: set[str] = set()
        for rank, hit in enumerate(retrieve(query), 1):
            key = record_id(hit)
            if not key or key in seen:
                continue
            seen.add(key)
            if key not in merged:
                merged[key] = {**hit, "rrf_score": 0.0, "retrieval_queries": []}
            merged[key]["rrf_score"] += 1.0 / (60 + rank)
            merged[key]["retrieval_queries"].append(query)
    return sorted(merged.values(), key=lambda hit: -hit["rrf_score"])[:limit]


class CompositionCandidateSource:
    """Adapt real composition search hits to the experimental matcher.

    The injected search targets one configured index/collection. A supplied
    catalogue resolves existing FoodEx2 codes as evidence, never as new facts.
    """

    def __init__(
        self, source_id: str, search: Callable[[str], list[dict]], *,
        foodex2: FoodEx2Catalogue | None = None,
        candidate_facets: Callable[[str, str], FoodFacets] | None = None,
    ) -> None:
        self.source_id = source_id
        self.search = search
        self.foodex2 = foodex2
        self.candidate_facets = candidate_facets

    @staticmethod
    def record_id(hit: dict) -> str:
        metadata = hit.get("metadata") or {}
        return str(metadata.get("canonical_food_id") or metadata.get("eu_food_id")
                   or metadata.get("eu_id") or metadata.get("food_id") or hit.get("id") or "")

    def retrieve(self, mention: FoodMention, limit: int) -> tuple[FoodCandidate, ...]:
        candidates = []
        for rank, hit in enumerate(retrieve_candidates(mention, self.search, self.record_id, limit), 1):
            label = str(hit.get("document") or "").strip()
            if not label:
                continue
            annotations = ()
            if self.foodex2 is not None:
                code = str((hit.get("metadata") or {}).get("foodex2_code") or "")
                annotations = (json.dumps(asdict(self.foodex2.resolve(code)), ensure_ascii=False),)
            candidates.append(FoodCandidate(
                record_id=self.record_id(hit), source_id=self.source_id, label=label,
                facets=(self.candidate_facets(self.record_id(hit), label)
                        if self.candidate_facets else extract_candidate_facets(label).facets),
                retrieval=RetrievalEvidence(
                    rank=rank, fused_score=hit["rrf_score"], queries=tuple(hit["retrieval_queries"]),
                ),
                annotation_evidence=annotations,
            ))
        return tuple(candidates)
