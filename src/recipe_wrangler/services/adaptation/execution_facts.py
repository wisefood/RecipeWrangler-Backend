"""Quote-linked equipment/state proposals for review, never automatic confirmations."""
from typing import Literal
import re

from pydantic import Field

from recipe_wrangler.repositories.matching_store import digest
from .models import ComparisonModel, Text
from .time_understanding import ActionIndex, TimeUnderstanding, text_hash, validate_understanding


class ResourceFact(ComparisonModel):
    resource: Text
    quote: Text
    basis: Literal["stated_mention", "inferred_method", "inferred_actor"]


class StateValue(ComparisonModel):
    item: Text
    state: Text


class StateFact(StateValue):
    quote: Text
    kind: Literal["required", "produced"]


class ActionFacts(ComparisonModel):
    action_id: ActionIndex
    resources: list[ResourceFact] = Field(default_factory=list)
    states: list[StateFact] = Field(default_factory=list)
    warnings: list[Text] = Field(default_factory=list)


class TimeHint(ComparisonModel):
    action_id: ActionIndex
    predecessor: ActionIndex | None
    kind: Literal["finish_offset", "during"]
    offset_minutes: float | None
    quote: Text
    approximate: bool = False


class ExecutionFacts(ComparisonModel):
    context_hash: Text
    understanding_hash: Text
    actions: list[ActionFacts]
    time_hints: list[TimeHint]
    initial_state_mentions: list[StateFact]
    warnings: list[Text] = Field(default_factory=list)
    facts_hash: Text
    review_required: Literal[True] = True


RESOURCE_WORDS = {
    "oven": r"\boven\b", "hob": r"\b(?:hob|stove|burner)\b",
    "pan": r"\b(?:pan|skillet)\b", "pot": r"\b(?:pot|saucepan|stockpot)\b",
    "bowl": r"\bbowl\b", "board": r"\b(?:board|work surface)\b",
    "knife": r"\bknife\b", "mixer": r"\b(?:mixer|blender|processor)\b",
    "microwave": r"\bmicrowave\b", "fridge": r"\b(?:fridge|refrigerator)\b",
    "freezer": r"\bfreezer\b", "grill": r"\bgrill\b",
    "slow_cooker": r"\bslow cooker\b", "air_fryer": r"\bair fryer\b",
}
STATES = "raw|cooked|chopped|sliced|mixed|coated|heated|cooled|drained|soaked|risen|baked"
VERB_STATES = {"chop": "chopped", "slice": "sliced", "mix": "mixed", "whisk": "mixed",
    "coat": "coated", "heat": "heated", "cool": "cooled", "drain": "drained",
    "soak": "soaked", "bake": "baked", "cook": "cooked", "simmer": "cooked"}
STOP_OBJECTS = {"a", "an", "and", "for", "in", "into", "of", "on", "or", "over", "to", "until", "with",
    "through", "enough", "slightly", "completely", "well", "briefly"}


def _state_mentions(text):
    found = []
    for match in re.finditer(rf"\b({STATES})\s+([a-z][a-z-]*)\b", text, re.I):
        if match[2].casefold() not in STOP_OBJECTS:
            found.append(StateFact(item=match[2].casefold(), state=match[1].casefold(), quote=match[0], kind="required"))
    verbs = "|".join(VERB_STATES)
    for match in re.finditer(rf"\b({verbs})\s+(?:the\s+)?(?:(?:{STATES})\s+)?([a-z][a-z-]*)\b", text, re.I):
        if match[2].casefold() not in STOP_OBJECTS:
            found.append(StateFact(item=match[2].casefold(), state=VERB_STATES[match[1].casefold()], quote=match[0], kind="produced"))
    return found


def propose_execution_facts(recipe: dict, understanding: TimeUnderstanding) -> ExecutionFacts:
    """Prefill source cues and expose missing facts; semantic completeness requires review."""
    understanding = validate_understanding(recipe, understanding)
    if len(understanding.actions) > 80:
        raise ValueError("execution_fact_budget_exceeded")
    actions, hints = [], []
    for action in understanding.actions:
        resources = []
        for resource, pattern in RESOURCE_WORDS.items():
            match = re.search(pattern, action.text, re.I)
            if match:
                resources.append(ResourceFact(resource=resource, quote=match[0], basis="stated_mention"))
        if action.attention == "active":
            resources.append(ResourceFact(resource="cook", quote=action.text.strip(), basis="inferred_actor"))
        if re.search(r"\b(?:bake|baking|roast|roasting)\b", action.text, re.I) and not any(
                fact.resource in {"oven", "microwave", "air_fryer"} for fact in resources):
            match = re.search(r"\b(?:bake|baking|roast|roasting)\b", action.text, re.I)
            resources.append(ResourceFact(resource="oven", quote=match[0], basis="inferred_method"))
        if re.search(r"\brefrigerate\b", action.text, re.I) and not any(f.resource == "fridge" for f in resources):
            match = re.search(r"\brefrigerate\b", action.text, re.I)
            resources.append(ResourceFact(resource="fridge", quote=match[0], basis="inferred_method"))
        warnings = []
        if action.attention in {"mixed", "unknown"}:
            warnings.append("attention_not_schedulable")
        if action.duration_minutes is None:
            warnings.append("complete_duration_missing")
        elif action.duration_minutes.lower != action.duration_minutes.upper:
            warnings.append("duration_range_requires_a_reviewed_exact_scenario")
        if re.search(r"\b(?:about|approximately|around|until|within|at least|up to)\b", action.text, re.I):
            warnings.append("qualified_or_outcome_based_duration")
        if not resources:
            warnings.append("resource_occupancy_requires_review")
        actions.append(ActionFacts(action_id=action.action_id, resources=resources,
            states=_state_mentions(action.text), warnings=warnings))
        offset = re.search(r"(?:(about)\s+)?(\d+(?:\.\d+)?)\s*(?:minutes?|min)\s+before\s+(?:the\s+)?end\b", action.text, re.I)
        concurrent = re.search(r"\b(?:meanwhile|while)\b", action.text, re.I)
        if offset or concurrent:
            prior = [a for a in understanding.actions[:action.action_id] if a.attention == "passive" and a.duration_minutes]
            predecessor = prior[-1].action_id if prior else None
            hints.append(TimeHint(action_id=action.action_id, predecessor=predecessor,
                kind="finish_offset" if offset else "during", offset_minutes=float(offset[2]) if offset else None,
                quote=(offset or concurrent)[0], approximate=bool(offset and offset[1])))
    initial = [mention for line in recipe["originals"] for mention in _state_mentions(line) if mention.kind == "required"]
    warnings = []
    data = dict(context_hash=text_hash(recipe), understanding_hash=digest(understanding.model_dump(mode="json")),
        actions=[item.model_dump(mode="json") for item in actions], time_hints=[item.model_dump(mode="json") for item in hints],
        initial_state_mentions=[item.model_dump(mode="json") for item in initial], warnings=warnings)
    return ExecutionFacts(**data, facts_hash=digest(data))
