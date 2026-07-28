"""Shared Elasticsearch mappings and cleaners for recipe evidence fields."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.utils.consumer_suitability import (
    SUPPORTED_CONSUMER_GROUPS,
)

RECIPE_EVIDENCE_MAPPING_PROPERTIES: dict[str, Any] = {
    "suitable_for": {"type": "keyword"},
    "allergen_evidence": {
        "type": "nested",
        "properties": {
            "allergen": {"type": "keyword"},
            "ingredient": {"type": "keyword"},
            "ingredient_id": {"type": "keyword"},
            "declaration_id": {"type": "keyword"},
            "presence": {"type": "keyword"},
            "evidence_status": {"type": "keyword"},
            "sources": {"type": "keyword"},
            "foodon_ids": {"type": "keyword"},
            "keyword_matches": {"type": "keyword"},
            "classification_version": {"type": "keyword"},
        },
    },
    "consumer_suitability": {
        "type": "nested",
        "properties": {
            "group": {"type": "keyword"},
            "status": {"type": "keyword"},
            "blocking_ingredients": {"type": "keyword"},
            "reason_codes": {"type": "keyword"},
            "sources": {"type": "keyword"},
            "classification_version": {"type": "keyword"},
        },
    },
}


def _clean_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _clean_lower(value: object) -> str:
    return _clean_str(value).lower()


def _clean_list(value: object, *, lower: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _clean_lower(raw) if lower else _clean_str(raw)
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output


def normalize_allergen_evidence(value: object) -> list[dict[str, Any]]:
    """Normalize and deterministically de-duplicate Neo4j declaration maps."""

    if not isinstance(value, (list, tuple)):
        return []
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        allergen = _clean_lower(raw.get("allergen"))
        ingredient = _clean_lower(raw.get("ingredient"))
        declaration_id = _clean_str(raw.get("declaration_id"))
        if not allergen or not ingredient:
            continue
        key = (allergen, ingredient, declaration_id)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "allergen": allergen,
                "ingredient": ingredient,
                "ingredient_id": _clean_str(raw.get("ingredient_id")),
                "declaration_id": declaration_id,
                "presence": _clean_lower(raw.get("presence")) or "contains",
                "evidence_status": (
                    _clean_lower(raw.get("evidence_status")) or "inferred"
                ),
                "sources": _clean_list(raw.get("sources")),
                "foodon_ids": _clean_list(
                    raw.get("foodon_ids"), lower=False
                ),
                "keyword_matches": _clean_list(raw.get("keyword_matches")),
                "classification_version": _clean_str(
                    raw.get("classification_version")
                ),
            }
        )
    return sorted(
        output,
        key=lambda item: (
            item["allergen"],
            item["ingredient"],
            item["declaration_id"],
        ),
    )


def normalize_consumer_suitability(
    value: object,
    *,
    classification_version: str,
) -> list[dict[str, Any]]:
    """Return exactly one vegan and one vegetarian three-state assessment."""

    by_group: dict[str, dict[str, Any]] = {}
    if isinstance(value, (list, tuple)):
        for raw in value:
            if not isinstance(raw, dict):
                continue
            group = _clean_lower(raw.get("group"))
            if group not in SUPPORTED_CONSUMER_GROUPS:
                continue
            status = _clean_lower(raw.get("status"))
            if status not in {"suitable", "not_suitable", "unknown"}:
                status = "unknown"
            by_group[group] = {
                "group": group,
                "status": status,
                "blocking_ingredients": _clean_list(
                    raw.get("blocking_ingredients")
                ),
                "reason_codes": _clean_list(raw.get("reason_codes")),
                "sources": _clean_list(raw.get("sources")),
                "classification_version": (
                    _clean_str(raw.get("classification_version"))
                    or classification_version
                ),
            }

    return [
        by_group.get(
            group,
            {
                "group": group,
                "status": "unknown",
                "blocking_ingredients": [],
                "reason_codes": ["missing_recipe_assessment"],
                "sources": [],
                "classification_version": classification_version,
            },
        )
        for group in SUPPORTED_CONSUMER_GROUPS
    ]


def suitable_groups(assessments: list[dict[str, Any]]) -> list[str]:
    """Return the exact positive shortcut represented by detailed statuses."""

    return [
        str(item["group"])
        for item in assessments
        if item.get("status") == "suitable"
    ]
