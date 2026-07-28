"""Canonical FATO/FoodOn identifiers and conservative suitability aggregation.

FATO provides the declaration and consumer-group vocabulary. FoodOn provides
the food taxonomy and label-claim identifiers. Neither ontology makes missing
knowledge equivalent to a negative fact, so suitability is deliberately
three-state: suitable, not_suitable, or unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CLASSIFICATION_VERSION = "fato-foodon-v1"
FATO_NAMESPACE = "http://www.w3id.org/FATO/"
FATO_ALLERGEN_CLASS_IRI = f"{FATO_NAMESPACE}Allergen"
FATO_ALLERGEN_DECLARATION_CLASS_IRI = (
    f"{FATO_NAMESPACE}AllergenDeclaration"
)


@dataclass(frozen=True)
class AllergenOntologyMapping:
    """Crosswalk for one internal allergen identifier."""

    foodon_label_claim_id: str
    eu_label: str


# FoodOn's label-claim branch follows the EU declaration groups. ``wheat`` is
# retained as an internal compatibility identifier, but maps to the broader
# cereals-containing-gluten claim instead of pretending it is a separate EU
# declaration group.
ALLERGEN_ONTOLOGY_MAPPINGS: dict[str, AllergenOntologyMapping] = {
    "gluten": AllergenOntologyMapping(
        "FOODON_03510214", "cereals containing gluten"
    ),
    "wheat": AllergenOntologyMapping(
        "FOODON_03510214", "cereals containing gluten"
    ),
    "crustacean_shellfish": AllergenOntologyMapping(
        "FOODON_03510215", "crustaceans"
    ),
    "egg": AllergenOntologyMapping("FOODON_03510216", "eggs"),
    "fish": AllergenOntologyMapping("FOODON_03510217", "fish"),
    "peanut": AllergenOntologyMapping("FOODON_03510218", "peanuts"),
    "soy": AllergenOntologyMapping("FOODON_03510219", "soybeans"),
    "milk": AllergenOntologyMapping("FOODON_03510220", "milk"),
    "tree_nut": AllergenOntologyMapping("FOODON_03510221", "nuts"),
    "celery": AllergenOntologyMapping("FOODON_03510222", "celery"),
    "mustard": AllergenOntologyMapping("FOODON_03510223", "mustard"),
    "sesame": AllergenOntologyMapping("FOODON_03510224", "sesame seeds"),
    "sulphites": AllergenOntologyMapping(
        "FOODON_03510225", "sulphur dioxide and sulphites"
    ),
    "lupin": AllergenOntologyMapping("FOODON_03510228", "lupin"),
    "molluscs": AllergenOntologyMapping("FOODON_03510232", "molluscs"),
}


CONSUMER_GROUP_IRIS: dict[str, str] = {
    "coeliac": f"{FATO_NAMESPACE}Coeliac",
    "vegan": f"{FATO_NAMESPACE}Vegan",
    "vegetarian": f"{FATO_NAMESPACE}Vegetarian",
}

SUITABILITY_STATUSES = frozenset({"suitable", "not_suitable", "unknown"})


def aggregate_recipe_suitability(
    evidence: Iterable[Mapping[str, Any]],
    *,
    ingredient_count: int,
    groups: Iterable[str] = CONSUMER_GROUP_IRIS,
) -> dict[str, str]:
    """Derive recipe suitability without treating missing facts as safe.

    ``evidence`` contains known ingredient-to-consumer-group assessments.
    A single ``not_suitable`` ingredient rejects the recipe. A recipe is
    ``suitable`` only when every ingredient has explicit suitable evidence.
    All incomplete or conflicting cases remain ``unknown``.
    """

    rows = list(evidence)
    result: dict[str, str] = {}
    for group in groups:
        group_rows = [
            row
            for row in rows
            if str(row.get("group") or "").strip().casefold() == group
        ]
        statuses = {
            str(row.get("status") or "").strip().casefold()
            for row in group_rows
        } & SUITABILITY_STATUSES
        if "not_suitable" in statuses:
            result[group] = "not_suitable"
            continue

        covered_ingredients = {
            str(row.get("ingredient_key") or "").strip()
            for row in group_rows
            if row.get("ingredient_key")
            and str(row.get("status") or "").strip().casefold() == "suitable"
        }
        if ingredient_count > 0 and len(covered_ingredients) == ingredient_count:
            result[group] = "suitable"
        else:
            result[group] = "unknown"
    return result
