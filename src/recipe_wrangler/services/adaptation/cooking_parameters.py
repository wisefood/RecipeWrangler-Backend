"""Validate bounded cooking-parameter edits without changing the input nutrition basis."""

import math
import re

from .models import AdaptationContext, CandidateAssessment
from .models import CookingAdjustment, InstructionPatch


NUMBER = r"\d+(?:\.\d+)?"
RANGE = rf"(?P<low>{NUMBER})(?:\s*(?:-|–|to)\s*(?P<high>{NUMBER}))?"
PATTERNS = {
    "duration": re.compile(rf"\b{RANGE}\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.I),
    "temperature": re.compile(rf"\b{RANGE}\s*(?:°\s*)?(?P<unit>celsius|fahrenheit|[CF])\b", re.I),
}
INTENTS = {"duration": re.compile(r"\b(?:time|timing|duration|overcook\w*|undercook\w*)\b", re.I),
           "temperature": re.compile(r"\b(?:temperature|heat)\b", re.I)}
COOKING = re.compile(r"\b(?:cook(?:ing|ed)?|bak(?:e|ing|ed)|roast(?:ing|ed)?|simmer(?:ing|ed)?|boil(?:ing|ed)?|grill(?:ing|ed)?|fr(?:y|ying|ied)|heat(?:ing|ed)?|brown(?:ing|ed)?|steam(?:ing|ed)?|poach(?:ing|ed)?)\b", re.I)

# A later-addition request cannot be fulfilled by changing a numeric duration.
SEQUENCING = re.compile(
    r"\b(?:reorder\w*|resequenc\w*|reschedul\w*|sequencing|delay\w*|earlier|later)\b"
    r"|\b(?:add|adding|return|returning|introduc\w*|remov\w*|mov\w*)\b[^.;]*\b(?:before|after|at the end|towards? the end)\b", re.I)


def cooking_parameter_slots(context: AdaptationContext, assessment: CandidateAssessment) -> list[dict]:
    """A conditional candidate advances only when every request has existing parameter slots.

    These edits preserve all words outside an existing duration/temperature. New
    methods, media, quantities, ingredients, yields and cooked-nutrition claims
    need separate evidence and operations.
    """
    if assessment.verdict != "suitable_with_changes" or not assessment.required_changes:
        return []
    step_positions = {step.step_id: index for index, step in enumerate(context.steps)}
    other_names = [item.name for item in context.ingredients if item.position != context.target_position]
    slots = []
    for index, change in enumerate(assessment.required_changes):
        if (change.kind not in {"instruction", "cooking_method"}
                or set(change.ingredient_positions) != {context.target_position}
                or not set(change.step_ids) <= step_positions.keys()
                or SEQUENCING.search(change.description)):
            return []
        parameters = [kind for kind, intent in INTENTS.items() if intent.search(change.description)]
        if not parameters:
            return []
        last = max(step_positions[step] for step in change.step_ids)
        found = set()
        for position, step in enumerate(context.steps):
            if step.step_id not in change.step_ids and (position < last or not COOKING.search(step.text)):
                continue
            # A later shared-pot step can carry an implicit target reference;
            # explicitly named retained ingredients require their own operation.
            if any(re.search(r"\b" + re.escape(name) + r"\b", step.text, re.I) for name in other_names):
                continue
            for kind in parameters:
                for match in PATTERNS[kind].finditer(step.text):
                    slots.append({"change_index": index, "parameter": kind, "reference_id": step.step_id,
                                  "start": match.start(), "end": match.end(), "before": match.group()})
                    found.add(kind)
        if found != set(parameters):
            return []
        durations = [slot for slot in slots if slot['change_index'] == index and slot['parameter'] == 'duration']
        if durations:
            try:
                longest = max(_values(slot['before'], 'duration')[1] for slot in durations)
                # Shortening browning alone cannot resolve a later long cooking phase.
                slots = [slot for slot in slots if slot not in durations
                         or _values(slot['before'], 'duration')[1] == longest]
            except ValueError:
                return []
    return slots


def _values(text: str, parameter: str) -> tuple[float, float]:
    match = PATTERNS[parameter].fullmatch(text)
    if match is None or len(text) > 64:
        raise ValueError("invalid_cooking_parameter_value")
    low, high = float(match['low']), float(match['high'] or match['low'])
    unit = match['unit'].casefold()
    if parameter == "duration":
        factor = 3600 if unit.startswith(('hour', 'hr')) else 60 if unit.startswith(('minute', 'min')) else 1
        low, high = low * factor, high * factor
        minimum, maximum = 0, 86400
    else:
        if unit in {'f', 'fahrenheit'}:
            low, high = (low - 32) * 5 / 9, (high - 32) * 5 / 9
        minimum, maximum = 0, 500
    if not (math.isfinite(low) and math.isfinite(high) and minimum < low <= high <= maximum):
        raise ValueError("invalid_cooking_parameter_range")
    return low, high


def validate_cooking_adjustments(context: AdaptationContext, assessment: CandidateAssessment,
                                 adjustments: list[CookingAdjustment]) -> list[InstructionPatch]:
    """Reused at editing and card assembly; model intent remains an inferred draft."""
    slots = cooking_parameter_slots(context, assessment)
    fields = ('change_index', 'parameter', 'reference_id', 'start', 'end', 'before')
    expected = {tuple(slot[key] for key in fields) for slot in slots}
    if assessment.verdict == 'suitable_with_changes' and not slots:
        raise ValueError('cooking_changes_not_executable')
    covered, used, patches = set(), set(), []
    for adjustment in adjustments:
        patch = adjustment.patch
        slot = {key: getattr(patch, key) for key in ('reference_id', 'start', 'end', 'before')}
        slot.update(change_index=adjustment.change_index, parameter=adjustment.parameter)
        span = (patch.reference_id, patch.start, patch.end)
        if patch.kind != 'instruction' or slot not in slots or span in used:
            raise ValueError('cooking_patch_outside_approved_parameters')
        if _values(patch.before, adjustment.parameter) == _values(patch.after, adjustment.parameter):
            raise ValueError('cooking_parameter_unchanged')
        used.add(span)
        covered.add(tuple(slot[key] for key in fields))
        patches.append(patch)
    if covered != expected:
        raise ValueError('required_cooking_changes_unresolved')
    return patches
