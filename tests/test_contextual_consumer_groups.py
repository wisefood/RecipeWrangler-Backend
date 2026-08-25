from datetime import date

from recipe_wrangler.utils.consumer_group_evidence import (
    IDDSI_TEXTURE_LEVELS,
    REQUIRED_PROVENANCE_FIELDS,
    assess_contextual_group,
    infant_age_band,
)


def _evidence(**overrides):
    row = {
        "scope": "product",
        "status": "suitable",
        "evidence_type": "certification",
        "authority": "accepted-body",
        "jurisdiction": "IE",
        "issuer": "accepted-body",
        "certification_identifier": "CERT-1",
        "valid_from": "2026-01-01",
        "valid_until": "2026-12-31",
        "rule_version": "policy-v1",
        "confidence": 1.0,
        "context": "ordinary",
    }
    row.update(overrides)
    return row


def test_provenance_schema_covers_required_governance_fields():
    assert set(REQUIRED_PROVENANCE_FIELDS) == {
        "authority", "jurisdiction", "issuer", "certification_identifier",
        "valid_from", "valid_until", "rule_version", "confidence",
    }


def test_absence_of_evidence_is_never_suitable():
    for group in ("halal", "kosher", "infant", "elderly"):
        assessment = assess_contextual_group(group)
        assert assessment.status == "unknown"


def test_halal_clear_negative_and_ambiguous_derivative_handling():
    assert assess_contextual_group("halal", ingredient_names=["pork loin"]).status == "not_suitable"
    ambiguous = assess_contextual_group("halal", ingredient_names=["gelatin powder"])
    assert ambiguous.status == "unknown"
    assert "requires_verification:gelatin" in ambiguous.reason_codes


def test_halal_suitable_requires_current_accepted_certification():
    evidence = [_evidence()]
    assert assess_contextual_group("halal", evidence=evidence).status == "unknown"
    assert assess_contextual_group(
        "halal",
        evidence=evidence,
        accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1),
    ).status == "suitable"


def test_expired_certification_returns_unknown():
    result = assess_contextual_group(
        "halal",
        evidence=[_evidence(valid_until="2025-12-31")],
        accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1),
    )
    assert result.status == "unknown"
    assert "expired_certification" in result.reason_codes


def test_conflicting_negative_evidence_wins_conservatively():
    result = assess_contextual_group(
        "kosher",
        evidence=[_evidence(), _evidence(status="not_suitable", evidence_type="inspection")],
        accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1),
    )
    assert result.status == "not_suitable"


def test_kosher_meat_dairy_and_passover_context_are_explicit():
    assert assess_contextual_group(
        "kosher", ingredient_names=["beef", "cream"]
    ).status == "not_suitable"
    passover = [_evidence(context="passover")]
    assert assess_contextual_group(
        "kosher", evidence=passover, accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1), context="ordinary",
    ).status == "unknown"
    assert assess_contextual_group(
        "kosher", evidence=passover, accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1), context="passover",
    ).status == "suitable"


def test_infant_requires_age_texture_and_age_specific_evidence():
    assert infant_age_band(7) == "6_to_8_months"
    assert assess_contextual_group(
        "infant", ingredient_names=["honey"], profile={"age_months": 7}
    ).status == "not_suitable"
    evidence = [_evidence(
        scope="recipe", evidence_type="clinical_assessment", age_band="6_to_8_months"
    )]
    result = assess_contextual_group(
        "infant",
        evidence=evidence,
        profile={"age_months": 7, "texture_assessment": "hse-stage-2"},
        accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1),
    )
    assert result.status == "suitable"


def test_elderly_is_profile_specific_not_age_wide():
    assert "IDDSI-6" in IDDSI_TEXTURE_LEVELS
    evidence = [_evidence(
        scope="recipe", evidence_type="clinical_assessment", profile_id="profile-1"
    )]
    incomplete = assess_contextual_group(
        "elderly", evidence=evidence, profile={"profile_id": "profile-1"},
        accepted_authorities=["accepted-body"], as_of=date(2026, 6, 1),
    )
    assert incomplete.status == "unknown"
    complete = assess_contextual_group(
        "elderly",
        evidence=evidence,
        profile={
            "profile_id": "profile-1",
            "nutrition_requirements": {"protein_g_min": 20},
            "texture_level": "IDDSI-6",
        },
        accepted_authorities=["accepted-body"],
        as_of=date(2026, 6, 1),
    )
    assert complete.status == "suitable"
