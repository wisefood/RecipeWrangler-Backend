"""Shared Elasticsearch mappings and cleaners for recipe evidence fields."""

from __future__ import annotations

from typing import Any

from recipe_wrangler.utils.food_ontology import ALLERGEN_ONTOLOGY_MAPPINGS, expand_allergen_exclusions

from recipe_wrangler.utils.consumer_suitability import (
    GROUP_RULES,
    SUPPORTED_CONSUMER_GROUPS,
    PESCATARIAN_CLASSIFICATION_VERSION,
    SUITABILITY_CLASSIFICATION_VERSION,
)

RECIPE_EVIDENCE_MAPPING_PROPERTIES: dict[str, Any] = {
    "suitable_for": {"type": "keyword"},
    "allergen_uncertainty": {"type": "boolean"},
    "allergen_assessment": {"type": "object", "enabled": False},
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
    seen: set[tuple[str, str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        allergen = _clean_lower(raw.get("allergen"))
        ingredient = _clean_lower(raw.get("ingredient"))
        declaration_id = _clean_str(raw.get("declaration_id"))
        if not allergen or not ingredient:
            continue
        presence = _clean_lower(raw.get("presence")) or "contains"
        # Keep conflicting statuses; input order must not erase a positive claim.
        key = (allergen, ingredient, declaration_id, presence)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "allergen": allergen,
                "ingredient": ingredient,
                "ingredient_id": _clean_str(raw.get("ingredient_id")),
                "declaration_id": declaration_id,
                "presence": presence,
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
            item["presence"],
        ),
    )


def normalize_consumer_suitability(
    value: object,
    *,
    classification_version: str,
    allergens: object = None,
    evidence: object = None,
) -> list[dict[str, Any]]:
    """Return one three-state assessment per supported composition group."""

    by_group: dict[str, dict[str, Any]] = {}
    current_by_group: dict[str, bool] = {}
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
            expected_version = PESCATARIAN_CLASSIFICATION_VERSION if group == "pescatarian" else classification_version
            current = raw.get("classification_version") == expected_version
            unversioned_positive = status == "suitable" and not current
            if unversioned_positive:
                status = "unknown"
            previous = by_group.get(group)
            severity = {"suitable": 0, "unknown": 1, "not_suitable": 2}
            if previous:
                if current_by_group[group] and not current:
                    continue
                if current_by_group[group] == current and severity[previous["status"]] >= severity[status]:
                    continue
            by_group[group] = {
                "group": group,
                "status": status,
                "blocking_ingredients": _clean_list(
                    raw.get("blocking_ingredients")
                ),
                "reason_codes": (["unsupported_classification_version"] if unversioned_positive
                                 else _clean_list(raw.get("reason_codes"))),
                "sources": _clean_list(raw.get("sources")),
                "classification_version": (
                    _clean_str(raw.get("classification_version"))
                    or classification_version
                ),
            }
            current_by_group[group] = current

    assessments = [
        by_group.get(
            group,
            {
                "group": group,
                "status": "unknown",
                "blocking_ingredients": [],
                "reason_codes": ["missing_recipe_assessment"],
                "sources": [],
                "classification_version": PESCATARIAN_CLASSIFICATION_VERSION if group == "pescatarian" else classification_version,
            },
        )
        for group in SUPPORTED_CONSUMER_GROUPS
    ]
    allergen_assessment = recipe_allergen_fields(allergens, evidence)["allergen_assessment"]
    detected = allergen_filter_groups(allergen_assessment["detected_allergens"])
    unknown = allergen_filter_groups([
        _clean_lower(row.get("allergen"))
        for row in evidence if isinstance(row, dict) and _clean_lower(row.get("presence")) == "unknown"
    ]) if isinstance(evidence, (list, tuple)) else set()
    for item in assessments:
        blocking = set(GROUP_RULES[item["group"]]["blocking_allergens"])
        if detected & blocking:
            item["status"] = "not_suitable"
            item["reason_codes"] = sorted((set(item["reason_codes"]) - {"all_ingredients_suitable"}) | {"allergen_evidence"})
            item["sources"] = sorted(set(item["sources"]) | {"allergen_evidence"})
        elif unknown & blocking and item["status"] == "suitable":
            item["status"] = "unknown"
            item["reason_codes"] = sorted((set(item["reason_codes"]) - {"all_ingredients_suitable"}) | {"unresolved_allergen_evidence"})
    return assessments


def suitable_groups(assessments: list[dict[str, Any]]) -> list[str]:
    """Return the exact positive shortcut represented by detailed statuses."""

    return [
        str(item["group"])
        for item in assessments
        if item.get("status") == "suitable"
    ]


def recipe_consumer_fields(value: object = None, *, allergens: object = None, evidence: object = None) -> dict[str, Any]:
    """Read current eligibility from assessments, never a stale positive shortcut."""
    assessments = normalize_consumer_suitability(
        value, classification_version=SUITABILITY_CLASSIFICATION_VERSION,
        allergens=allergens, evidence=evidence,
    )
    return {"consumer_suitability": assessments, "suitable_for": suitable_groups(assessments)}


def assessed_diet_tags(
    value: object, assessments: list[dict[str, Any]], *,
    allergens: object = None, evidence: object = None,
) -> list[str]:
    """Keep source labels separate from derived vegan/vegetarian eligibility."""
    from recipe_wrangler.utils.diet_tags import ALLERGEN_ABSENCE_RULES

    positive = {"pescatarian_safe" if group == "pescatarian" else group for group in suitable_groups(assessments)}
    tags = (set(_clean_list(value)) - set(SUPPORTED_CONSUMER_GROUPS) - {"pescatarian_safe"}) | positive
    allergen_assessment = recipe_allergen_fields(allergens, evidence)['allergen_assessment']
    known = allergen_filter_groups(allergen_assessment['detected_allergens'] + allergen_assessment['possible_allergens'])
    return sorted(tag for tag in tags if not known.intersection(ALLERGEN_ABSENCE_RULES.get(tag, [])))


def recipe_allergen_fields(allergens: object = None, evidence: object = None) -> dict[str, Any]:
    """Report presence evidence without turning missing knowledge into absence.

    Ingredient-level absence declarations cannot establish whole-recipe absence.
    Cross-contact is not assessed by recipe text or food-name detection.
    """
    detected = set(_clean_list(allergens))
    possible: set[str] = set()
    for item in evidence if isinstance(evidence, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        allergen = _clean_lower(item.get("allergen"))
        if not allergen:
            continue
        presence = _clean_lower(item.get("presence")) or "contains"
        if presence in {"contains", "present"}:
            detected.add(allergen)
        elif presence in {"possible", "may_contain"}:
            possible.add(allergen)
    unresolved = set(ALLERGEN_ONTOLOGY_MAPPINGS) - detected
    possible -= detected
    reasons = ["allergen_absence_not_assessed"] if unresolved else []
    if possible:
        reasons.append("possible_allergen_presence")
    return {
        "allergen_uncertainty": bool(unresolved or possible),
        "allergen_assessment": {
            "version": "recipe_allergens_v1",
            "detected_allergens": sorted(detected),
            "possible_allergens": sorted(possible),
            "unresolved_allergens": sorted(unresolved),
            "reason_codes": reasons,
            "cross_contact_assessed": False,
        },
    }


# Equivalent label spellings; umbrella requests expand without claiming that
# an ambiguous stored "nuts" label proves peanut or tree-nut presence.
_ALLERGEN_LABEL_FAMILIES = {
    "milk": {"milk", "dairy"},
    "egg": {"egg", "eggs"},
    "peanut": {"peanut", "peanuts", "groundnut", "groundnuts"},
    "tree_nut": {"tree_nut", "tree_nuts", "tree nut", "tree nuts"},
    "soy": {"soy", "soya", "soybean", "soybeans"},
    "crustacean_shellfish": {"crustacean_shellfish", "crustacean", "crustaceans"},
    "molluscs": {"molluscs", "mollusc", "mollusks", "mollusk"},
    "sulphites": {"sulphites", "sulphite", "sulfites", "sulfite"},
}
_ALLERGEN_UMBRELLAS = {
    "nut": {"peanut", "tree_nut"}, "nuts": {"peanut", "tree_nut"},
    "shellfish": {"crustacean_shellfish", "molluscs"},
}


def allergen_filter_groups(values: list[str]) -> set[str]:
    """Resolve request spellings; preserve unrecognized identifiers verbatim.

    Requests are folded through the shared alias table first (the one the UI,
    gateway and FoodChat copy: lactose -> milk, gluten implies wheat), then the
    stored-label families below; the union is excluded.
    """
    output: set[str] = set()
    for value in _clean_list([*_clean_list(values), *expand_allergen_exclusions(values)]):
        key = value.replace("-", "_").replace(" ", "_")
        if key in _ALLERGEN_UMBRELLAS:
            output.update(_ALLERGEN_UMBRELLAS[key])
            continue
        output.add(next((group for group, aliases in _ALLERGEN_LABEL_FAMILIES.items()
                         if value in aliases or key in aliases), key))
    return output


def allergen_filter_values(values: list[str]) -> list[str]:
    """Match equivalent stored labels and the explicitly requested umbrella."""
    output = set(_clean_list(values))
    for group in allergen_filter_groups(values):
        output.update(_ALLERGEN_LABEL_FAMILIES.get(group, {group}))
    return sorted(output)


def known_allergen_exclusions(allergens: list[str]) -> list[dict[str, Any]]:
    """Exclude recorded positive conflicts; unknown/possible evidence stays visible."""
    values = allergen_filter_values(allergens)
    if not values:
        return []
    return [
        {"terms": {"allergens": values}},
        {"nested": {
            "path": "allergen_evidence", "ignore_unmapped": True,
            "query": {"bool": {"filter": [
                {"terms": {"allergen_evidence.allergen": values}},
                {"bool": {"should": [
                    {"terms": {"allergen_evidence.presence": ["contains", "present"]}},
                    {"term": {"allergen_evidence.presence": ""}},
                    {"bool": {"must_not": [{"exists": {"field": "allergen_evidence.presence"}}]}},
                ], "minimum_should_match": 1}},
            ]}},
        }},
    ]
