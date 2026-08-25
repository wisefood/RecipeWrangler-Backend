"""The `nutrition_claims` facet -- canonical tag names.

Single source of truth shared between scripts/facets/tag_nutrition_claims.py
(computes and writes the Tag nodes) and the catalog projection
(catalog/projection.py, scripts/catalog/build_recipes.py -- reads them into
the `nutrition_claims` ES field), so the ES field only ever surfaces tags
this facet's rules actually produce, not whatever else later lands under the
same Neo4j category.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

EU_CLAIMS_REGULATION_URL = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32006R1924"
)
LOW_ENERGY_PER_100_SOLID = 40.0
LOW_ENERGY_PER_100_LIQUID = 20.0
LOW_FAT_PER_100_SOLID = 3.0
LOW_FAT_PER_100_LIQUID = 1.5
HIGH_FIBRE_PER_100G = 6.0
HIGH_FIBRE_PER_100KCAL = 3.0
HIGH_PROTEIN_ENERGY_FRACTION = 0.20

_LIQUID_TITLE_RE = re.compile(
    r"\b(?:smoothie|milkshake|juice|lemonade|mocktail|cocktail|beverage|drink|punch)\b",
    re.IGNORECASE,
)


def infer_physical_form(title: object, course_types: Iterable[object] = ()) -> str:
    if any(str(value).strip().lower() == "beverages" for value in course_types):
        return "liquid"
    return "liquid" if _LIQUID_TITLE_RE.search(str(title or "")) else "solid"

NUTRITION_CLAIM_TAG_NAMES: tuple[str, ...] = (
    "low_calorie", "low_fat", "high_fibre", "high_protein", "healthy_and_nutritious",
)


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _nutri_grade(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("nutri_score") or value.get("label")
    text = str(value or "").strip()
    if not text:
        return None
    grade = text.rsplit("_", 1)[-1].upper()
    return grade if grade in {"A", "B", "C", "D", "E"} else None


def compute_nutrition_claim_tags(
    total_nutrients: dict[str, Any] | None,
    ingredient_details: Iterable[dict[str, Any]] | None,
    nutri_score: object = None,
    physical_form: str = "solid",
) -> list[str]:
    """Derive the v4 nutrition claims for one completed profile.

    Per-100g claims require a positive calculated recipe weight. Missing
    nutrient values remain unknown and never satisfy a threshold by being
    coerced to zero. ``healthy_and_nutritious`` follows the established corpus
    rule: the selected authoritative profile has Nutri-Score A.
    """
    nutrients = total_nutrients if isinstance(total_nutrients, dict) else {}
    details = [item for item in (ingredient_details or []) if isinstance(item, dict)]
    total_weight = sum(
        max(0.0, _number(item.get("weight_g") or item.get("weight")) or 0.0)
        for item in details
    )

    kcal = _number(nutrients.get("energy_kcal"))
    fat = _number(nutrients.get("fat_g"))
    fibre = _number(nutrients.get("fibre_g"))
    if fibre is None:
        fibre = _number(nutrients.get("fiber_g"))
    protein = _number(nutrients.get("protein_g"))

    tags: list[str] = []
    if total_weight > 0:
        factor = 100.0 / total_weight
        liquid = str(physical_form).strip().lower() == "liquid"
        low_energy_limit = (
            LOW_ENERGY_PER_100_LIQUID if liquid else LOW_ENERGY_PER_100_SOLID
        )
        low_fat_limit = LOW_FAT_PER_100_LIQUID if liquid else LOW_FAT_PER_100_SOLID
        if kcal is not None and kcal * factor <= low_energy_limit:
            tags.append("low_calorie")
        if fat is not None and fat * factor <= low_fat_limit:
            tags.append("low_fat")
        if fibre is not None:
            fibre_per_100g = fibre * factor >= HIGH_FIBRE_PER_100G
            fibre_per_100kcal = (
                kcal is not None
                and kcal > 0
                and fibre * 100.0 / kcal >= HIGH_FIBRE_PER_100KCAL
            )
            if fibre_per_100g or fibre_per_100kcal:
                tags.append("high_fibre")

    if protein is not None and kcal is not None and kcal > 0:
        if protein * 4.0 / kcal >= HIGH_PROTEIN_ENERGY_FRACTION:
            tags.append("high_protein")

    if _nutri_grade(nutri_score) == "A":
        tags.append("healthy_and_nutritious")

    return [name for name in NUTRITION_CLAIM_TAG_NAMES if name in tags]
