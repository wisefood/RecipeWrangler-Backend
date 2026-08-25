"""Conservative evidence model for FATO's four context-sensitive groups.

FATO supplies vocabulary, not a universal decision procedure. In particular,
``isSuitableFor`` has Product as its formal domain. Ingredient and Recipe
assessments here are explicit Recipe Wrangler application extensions and carry
their scope so they cannot be mistaken for certified product claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, Iterable, Mapping

CONTEXTUAL_GROUPS = ("halal", "kosher", "infant", "elderly")
EVIDENCE_SCOPES = frozenset({"ingredient", "recipe", "product", "process", "user_profile"})
EVIDENCE_STATUSES = frozenset({"suitable", "not_suitable", "unknown"})
REQUIRED_PROVENANCE_FIELDS = (
    "authority",
    "jurisdiction",
    "issuer",
    "certification_identifier",
    "valid_from",
    "valid_until",
    "rule_version",
    "confidence",
)
IDDSI_TEXTURE_LEVELS = frozenset(f"IDDSI-{level}" for level in range(8))

HALAL_NEGATIVE_TERMS = frozenset({
    "pork", "bacon", "ham", "lard", "gelatin pork", "blood", "alcohol",
    "wine", "beer", "rum", "brandy",
})
HALAL_AMBIGUOUS_TERMS = frozenset({
    "gelatin", "gelatine", "rennet", "enzyme", "enzymes", "flavouring",
    "flavoring", "processing aid", "glycerin", "glycerine", "mono glyceride",
})
KOSHER_PROHIBITED_TERMS = frozenset({
    "pork", "bacon", "ham", "lard", "shellfish", "shrimp", "prawn",
    "lobster", "crab", "oyster", "mussel", "clam",
})
KOSHER_AMBIGUOUS_TERMS = frozenset({
    "gelatin", "gelatine", "rennet", "enzyme", "enzymes", "flavouring",
    "flavoring", "processing aid", "glycerin", "glycerine",
})
MEAT_TERMS = frozenset({
    "beef", "veal", "lamb", "mutton", "goat", "chicken", "turkey", "duck",
    "meat",
})
DAIRY_TERMS = frozenset({
    "milk", "cheese", "butter", "cream", "yogurt", "yoghurt", "whey", "casein",
})

DEFINITION_SOURCES: dict[str, tuple[str, ...]] = {
    "fato": ("https://w3id.org/FATO",),
    "halal": (
        "https://ask.fsis.usda.gov/article/askFSIS-Public-Q-A-Halal-Kosher",
    ),
    "kosher": (
        "https://oukosher.org/kosher-overview/steps-to-kosher-certification/",
    ),
    "infant": (
        "https://www2.hse.ie/babies-children/weaning-eating/weaning/stages/",
        "https://www2.hse.ie/babies-children/child-safety/choking-strangulation-suffocation/food-choking-risks/",
    ),
    "elderly": (
        "https://www.who.int/tools/elena/interventions/nutrition-older-people",
        "https://www.iddsi.org/standards/framework",
    ),
}


@dataclass(frozen=True)
class ConsumerAssessment:
    group: str
    status: str
    reason_codes: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    classification_version: str = "contextual-consumer-evidence-v1"


@dataclass(frozen=True)
class ContextualConsumerEvidence:
    """Evidence envelope shared by ingredient, recipe, product and profile scopes."""

    scope: str
    status: str
    evidence_type: str
    authority: str | None = None
    jurisdiction: str | None = None
    issuer: str | None = None
    certification_identifier: str | None = None
    valid_from: date | str | None = None
    valid_until: date | str | None = None
    rule_version: str | None = None
    confidence: float | None = None
    # Religious-policy context.
    dietary_category: str | None = None  # meat, dairy, pareve
    slaughter_method: str | None = None
    equipment_status: str | None = None
    cross_contact_status: str | None = None
    context: str = "ordinary"  # Passover is a separate contextual claim.
    # Infant evidence dimensions.
    age_band: str | None = None
    preparation_assessment: str | None = None
    texture_assessment: str | None = None
    portion_assessment: str | None = None
    allergen_assessment: str | None = None
    microbiological_assessment: str | None = None
    nutrition_assessment: str | None = None
    # Older-person/profile evidence dimensions.
    profile_id: str | None = None
    texture_level: str | None = None
    swallowing_assessment: str | None = None
    sodium_assessment: str | None = None
    preparation_safety: str | None = None
    medical_restrictions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def infant_age_band(age_months: int | None) -> str | None:
    if age_months is None or age_months < 0:
        return None
    if age_months < 6:
        return "under_6_months"
    if age_months < 9:
        return "6_to_8_months"
    if age_months < 12:
        return "9_to_11_months"
    if age_months < 24:
        return "12_to_23_months"
    return "24_months_plus"


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _contains_term(names: Iterable[str], terms: Iterable[str]) -> list[str]:
    normalized = [str(name).strip().casefold() for name in names]
    return sorted({
        term
        for term in terms
        if any(
            re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text)
            for text in normalized
        )
    })


def _active_evidence(
    evidence: Iterable[Mapping[str, Any] | ContextualConsumerEvidence], *, as_of: date
) -> tuple[list[dict[str, Any]], list[str]]:
    active: list[dict[str, Any]] = []
    reasons: list[str] = []
    for raw in evidence:
        row = raw.as_dict() if isinstance(raw, ContextualConsumerEvidence) else dict(raw)
        if row.get("scope") not in EVIDENCE_SCOPES or row.get("status") not in EVIDENCE_STATUSES:
            reasons.append("invalid_evidence_shape")
            continue
        valid_from = _as_date(row.get("valid_from"))
        valid_until = _as_date(row.get("valid_until"))
        if row.get("valid_from") not in (None, "") and valid_from is None:
            reasons.append("invalid_valid_from")
            continue
        if row.get("valid_until") not in (None, "") and valid_until is None:
            reasons.append("invalid_valid_until")
            continue
        if valid_from and as_of < valid_from:
            reasons.append("certification_not_yet_valid")
            continue
        if valid_until and as_of > valid_until:
            reasons.append("expired_certification")
            continue
        active.append(row)
    return active, reasons


def _accepted_certification(
    rows: Iterable[dict[str, Any]], *, accepted_authorities: set[str], context: str
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for row in rows:
        authority = str(row.get("authority") or row.get("issuer") or "").strip()
        complete = all(row.get(field) not in (None, "") for field in REQUIRED_PROVENANCE_FIELDS)
        row_context = str(row.get("context") or "ordinary")
        if (
            row.get("evidence_type") == "certification"
            and row.get("scope") in {"recipe", "product", "process"}
            and row.get("status") == "suitable"
            and complete
            and authority in accepted_authorities
            and row_context == context
        ):
            accepted.append(row)
    return accepted


def _complete_authoritative_assessment(
    row: Mapping[str, Any], accepted_authorities: set[str]
) -> bool:
    authority = str(row.get("authority") or row.get("issuer") or "").strip()
    return (
        authority in accepted_authorities
        and all(row.get(field) not in (None, "") for field in REQUIRED_PROVENANCE_FIELDS)
    )


def assess_contextual_group(
    group: str,
    *,
    ingredient_names: Iterable[str] = (),
    evidence: Iterable[Mapping[str, Any] | ContextualConsumerEvidence] = (),
    profile: Mapping[str, Any] | None = None,
    accepted_authorities: Iterable[str] = (),
    context: str = "ordinary",
    as_of: date | None = None,
) -> ConsumerAssessment:
    """Assess one contextual FATO group without turning missing data into safety."""
    group = str(group).strip().casefold()
    if group not in CONTEXTUAL_GROUPS:
        raise ValueError(f"unsupported contextual consumer group: {group}")
    today = as_of or date.today()
    active, evidence_reasons = _active_evidence(evidence, as_of=today)
    reasons = list(dict.fromkeys(evidence_reasons))

    explicit_negative = [row for row in active if row.get("status") == "not_suitable"]
    if explicit_negative:
        return ConsumerAssessment(
            group, "not_suitable", ("explicit_negative_evidence",), tuple(explicit_negative)
        )

    names = list(ingredient_names)
    if group == "halal":
        blockers = _contains_term(names, HALAL_NEGATIVE_TERMS)
        if blockers:
            return ConsumerAssessment(group, "not_suitable", tuple(f"contains_{x}" for x in blockers), ())
        ambiguous = _contains_term(names, HALAL_AMBIGUOUS_TERMS)
        if ambiguous:
            reasons.extend(f"requires_verification:{x}" for x in ambiguous)

    if group == "kosher":
        blockers = _contains_term(names, KOSHER_PROHIBITED_TERMS)
        if blockers:
            return ConsumerAssessment(group, "not_suitable", tuple(f"contains_{x}" for x in blockers), ())
        if _contains_term(names, MEAT_TERMS) and _contains_term(names, DAIRY_TERMS):
            return ConsumerAssessment(group, "not_suitable", ("meat_dairy_combination",), ())
        ambiguous = _contains_term(names, KOSHER_AMBIGUOUS_TERMS)
        if ambiguous:
            reasons.extend(f"requires_verification:{x}" for x in ambiguous)

    accepted = _accepted_certification(
        active, accepted_authorities=set(accepted_authorities), context=context
    )
    if group in {"halal", "kosher"}:
        if accepted and not reasons:
            return ConsumerAssessment(group, "suitable", ("accepted_current_certification",), tuple(accepted))
        reasons.append("accepted_certification_required")

    if group == "infant":
        profile = dict(profile or {})
        age_months = profile.get("age_months")
        age_band = infant_age_band(age_months if isinstance(age_months, int) else None)
        if not age_band:
            reasons.append("infant_age_band_required")
        elif age_months < 12 and _contains_term(names, {"honey"}):
            return ConsumerAssessment(group, "not_suitable", ("honey_under_12_months",), ())
        elif age_months < 12 and _contains_term(names, {"salt", "stock cube", "gravy"}):
            return ConsumerAssessment(group, "not_suitable", ("added_salt_under_12_months",), ())
        if not profile.get("texture_assessment"):
            reasons.append("texture_choking_assessment_required")
        supported = [
            row for row in active
            if row.get("status") == "suitable"
            and row.get("scope") == "recipe"
            and row.get("age_band") == age_band
            and _complete_authoritative_assessment(row, set(accepted_authorities))
        ]
        if supported and not reasons:
            return ConsumerAssessment(group, "suitable", ("age_specific_authoritative_assessment",), tuple(supported))

    if group == "elderly":
        profile = dict(profile or {})
        if not profile.get("nutrition_requirements"):
            reasons.append("individual_nutrition_profile_required")
        if not profile.get("texture_level"):
            reasons.append("texture_swallowing_profile_required")
        elif profile.get("texture_level") not in IDDSI_TEXTURE_LEVELS:
            reasons.append("unsupported_texture_classification")
        supported = [
            row for row in active
            if row.get("status") == "suitable"
            and row.get("scope") == "recipe"
            and row.get("profile_id") == profile.get("profile_id")
            and _complete_authoritative_assessment(row, set(accepted_authorities))
        ]
        if supported and not reasons:
            return ConsumerAssessment(group, "suitable", ("profile_specific_clinical_assessment",), tuple(supported))

    return ConsumerAssessment(
        group,
        "unknown",
        tuple(dict.fromkeys(reasons or ["insufficient_evidence"])),
        tuple(active),
    )
