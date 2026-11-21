"""Recover quoted source claims without editing saved observations or masses."""

from functools import lru_cache
from copy import deepcopy
import re

from recipe_wrangler.matching.alternatives import selected_source_branch
from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
from recipe_wrangler.matching.quantities import resolve_quantity
from recipe_wrangler.repositories.matching_store import digest

from .models import Evidence, OccurrenceSourceEvidence, PropertyAssertion


@lru_cache(maxsize=1)
def _annotator():
    from recipe_wrangler.matching.runtime import build_annotator
    return build_annotator().annotate


def recover_source_evidence(detail: dict, *, recipe_id: str, run_id: str,
                            quantity_hash: str) -> OccurrenceSourceEvidence | None:
    """Recover source claims independently of whether the line verifies a mass."""
    evidence = detail.get("quantity_evidence") or {}
    quantity_line = evidence.get("source_line") if evidence.get("kind") in {"explicit_source_mass", "source_volume_estimate"} else None
    line = quantity_line or detail.get("original_ingredient_text")
    source_field = "quantity_evidence/source_line" if quantity_line else "original_ingredient_text"
    if not isinstance(line, str) or not line.strip() or detail["weight_g"] <= 0:
        return None
    name = detail.get("ingredient") or detail.get("name")
    overlay = OccurrenceSourceEvidence(recipe_id=recipe_id, baseline_run_id=run_id,
        quantity_input_hash=quantity_hash, position=detail["position"], weight_g=detail["weight_g"],
        source_line=line, source_hash=digest(line))
    selected = selected_source_branch(name, line)
    if selected:
        overlay.selected_source_text = selected
    try:
        if quantity_line:
            checked = resolve_quantity(name, detail["weight_g"], evidence=evidence)
            if checked["quantity_evidence"] is None or checked["weight_g"] != detail["weight_g"]:
                raise ValueError("source_line_does_not_establish_saved_mass")
    except (ValueError, TypeError, KeyError):
        overlay.deferred_reason = "source_quantity_evidence_invalid"
        return overlay
    try:
        facts = recover_occurrence_facts(name, selected or line, _annotator())
    except RuntimeError:
        overlay.deferred_reason = "source_annotation_assets_unavailable"
        return overlay
    recovery = facts["occurrence_recovery"]
    overlay.deferred_reason = recovery["deferred_reason"]
    overlay.conflicts = [item["field"] for item in recovery["conflicts"]]
    for claim in recovery["recovered"]:
        supports = claim["evidence"]
        if selected:
            offset = line.index(selected)
            supports = deepcopy(supports)
            for support in supports:
                for span in support.get("spans", []):
                    span["start"] += offset
                    span["end"] += offset
        if not supports or not all(
            support.get("status") == "stated" and support.get("spans") and all(
                line[span["start"]:span["end"]] == support["quote"] for span in support["spans"]
            ) for support in supports
        ):
            continue
        value = claim["value"]
        if claim["field"] in {"skin_status", "bone_status"} and value == "absent":
            value = "skinless" if claim["field"] == "skin_status" else "boneless"
        overlay.properties[claim["field"]] = PropertyAssertion(value=value, evidence=Evidence(
            reference=f"matching_profile_runs/{run_id}/profile/details/{detail['position']}/{source_field}#{claim['field']}",
            method="quoted_source_line_recovery", version=overlay.recovery_version, status="stated"))
        overlay.property_support[claim["field"]] = deepcopy(supports)
    # These literal preparation phrases are often absent from saved name parses.
    # A request to remove skin/bone later does not describe the current ingredient.
    if overlay.deferred_reason or overlay.conflicts:
        return overlay
    saved_identity = (detail.get('observed_facts') or {}).get('facets', {}).get('base_identity')
    if saved_identity in (None, '', 'unknown') and facts.get('main_ingredient') and len(facts.get('foods') or []) == 1:
        supports = [item for item in facts.get('evidence', [])
                    if item.get('field') == 'main_ingredient' and item.get('polarity') == 'asserted'
                    and item.get('quote')]
        for support in supports:
            quote = support['quote']
            matches = list(re.finditer(r'(?<!\w)' + re.escape(quote) + r'(?!\w)', selected or line))
            if len(matches) != 1:
                continue
            start = (line.index(selected) if selected else 0) + matches[0].start()
            overlay.properties['base_identity'] = PropertyAssertion(value=facts['main_ingredient'], evidence=Evidence(
                reference=f"matching_profile_runs/{run_id}/profile/details/{detail['position']}/{source_field}#base_identity",
                method='quoted_dictionary_identity_recovery', version='quoted_identity_overlay_v1', status='stated'))
            overlay.property_support['base_identity'] = [{'quote': quote, 'status': 'stated',
                'spans': [{'start': start, 'end': start + len(quote)}],
                'concept_candidates': facts['foods'][0].get('concept_candidates', [])}]
            break
    scope = selected or line
    for match in re.finditer(r"\b(?:skin(?:\s+and\s+bones?)?|bones?(?:\s+and\s+skin)?)\s+removed\b", scope, re.I):
        if re.search(r"\b(?:to|not|never)\b[^,;()]*$", scope[:match.start()], re.I):
            continue
        quote = match.group()
        start = line.index(scope) + match.start()
        for field, value in (("skin_status", "skinless"), ("bone_status", "boneless")):
            if not re.search(r"\b" + ("skin" if field == "skin_status" else "bones?") + r"\b", quote, re.I):
                continue
            existing = overlay.properties.get(field)
            if existing and existing.value != value:
                overlay.conflicts.append(field)
                continue
            overlay.properties[field] = PropertyAssertion(value=value, evidence=Evidence(
                reference=f"matching_profile_runs/{run_id}/profile/details/{detail['position']}/{source_field}#{field}",
                method="quoted_source_line_recovery", version=overlay.recovery_version, status="stated"))
            overlay.property_support[field] = [{"quote": quote, "status": "stated",
                "spans": [{"start": start, "end": start + len(quote)}]}]
    return overlay
