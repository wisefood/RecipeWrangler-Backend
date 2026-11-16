"""Bounded, non-preemptive whole-recipe scheduling under explicit reviewed facts."""
from typing import Annotated, Literal
import math

from pydantic import Field

from recipe_wrangler.repositories.matching_store import digest
from .models import ComparisonModel, Text
from .execution_facts import ExecutionFacts, StateValue, propose_execution_facts
from .time_understanding import ActionIndex, TimeUnderstanding, resource_keys, validate_understanding

Minute = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonnegativeMinute = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
EPS = 1e-8


class ActionReview(ComparisonModel):
    action_id: ActionIndex
    resources: set[Text] = Field(default_factory=set)
    required_states: list[StateValue] = Field(default_factory=list)
    produced_states: list[StateValue] = Field(default_factory=list)
    timing_scope_confirmed: bool = Field(default=False, strict=True)
    completion_confirmed: bool = Field(default=False, strict=True)
    handoff_action_id: ActionIndex | None = None


class TemporalConstraint(ComparisonModel):
    predecessor: ActionIndex
    successor: ActionIndex
    relation: Literal["finish_start", "start_start", "during"] = "finish_start"
    min_lag_minutes: Minute = 0.0
    max_lag_minutes: Minute | None = None
    source_action_id: ActionIndex | None = None
    quote: str | None = None


class Placement(ComparisonModel):
    action_id: ActionIndex
    start_minutes: NonnegativeMinute
    end_minutes: NonnegativeMinute


class ScheduleReview(ComparisonModel):
    """Complete semantic/resource/timing review is external to model proposals."""
    context_hash: Text
    understanding_hash: Text
    facts_hash: Text
    confirmed: bool = Field(default=False, strict=True)
    reviewed_by: Text | None = None
    actions: list[ActionReview] = Field(max_length=80)
    constraints: list[TemporalConstraint] = Field(default_factory=list, max_length=400)
    initial_states: list[StateValue] = Field(default_factory=list)
    baseline_mode: Literal["unknown", "reviewed_serial", "provided"] = "unknown"
    baseline: list[Placement] = Field(default_factory=list, max_length=80)


class ScheduledAction(Placement):
    step_id: Text
    source_text: str
    resources: set[Text]


class MealSchedule(ComparisonModel):
    status: Literal["needs_evidence", "improved_under_reviewed_assumptions", "no_verified_improvement"] = "needs_evidence"
    reasons: list[Text] = Field(default_factory=list)
    original_instructions: list[str]
    baseline: list[ScheduledAction] = Field(default_factory=list)
    proposed: list[ScheduledAction] = Field(default_factory=list)
    baseline_minutes: NonnegativeMinute | None = None
    proposed_minutes: NonnegativeMinute | None = None
    estimated_saving_minutes: NonnegativeMinute | None = None
    review_hash: Text | None = None
    recipe_ready: Literal[False] = False
    assumptions: list[Text] = Field(default_factory=list)


def make_schedule_review(facts: ExecutionFacts, understanding: TimeUnderstanding) -> ScheduleReview:
    """Prefill an unconfirmed review pack; no inferred field is an approval."""
    actions = [ActionReview(action_id=f.action_id, resources={r.resource for r in f.resources},
        required_states=[StateValue(item=s.item, state=s.state) for s in f.states if s.kind == "required"],
        produced_states=[StateValue(item=s.item, state=s.state) for s in f.states if s.kind == "produced"])
        for f in facts.actions]
    constraints = [TemporalConstraint(predecessor=parent, successor=action.action_id)
        for action in understanding.actions for parent in action.depends_on]
    return ScheduleReview(context_hash=facts.context_hash, understanding_hash=facts.understanding_hash,
        facts_hash=facts.facts_hash, actions=actions, constraints=constraints,
        initial_states=[StateValue(item=s.item, state=s.state) for s in facts.initial_state_mentions])


def _state_constraints(review):
    state, previous, edges = {}, {}, []
    for value in review.initial_states:
        key = value.item.casefold()
        if key in state and state[key] != value.state.casefold():
            raise ValueError("conflicting_initial_states")
        state[key] = value.state.casefold()
    for action in sorted(review.actions, key=lambda a: a.action_id):
        keys = {value.item.casefold() for value in action.required_states + action.produced_states}
        for key in keys:
            if key in previous:
                edges.append(TemporalConstraint(predecessor=previous[key], successor=action.action_id))
            previous[key] = action.action_id
        for value in action.required_states:
            if state.get(value.item.casefold()) != value.state.casefold():
                raise ValueError(f"required_physical_state_unavailable:action:{action.action_id}")
        effects = {}
        for value in action.produced_states:
            key = value.item.casefold()
            if key in effects and effects[key] != value.state.casefold():
                raise ValueError("conflicting_action_effects")
            effects[key] = value.state.casefold()
        state.update(effects)
    return edges


def _source_windows(facts, constraints):
    for hint in facts.time_hints:
        if hint.approximate:
            raise ValueError("approximate_source_window_requires_reviewed_timing")
        matches = [c for c in constraints if c.successor == hint.action_id
            and c.source_action_id == hint.action_id and c.quote == hint.quote]
        if hint.kind == "finish_offset":
            matches = [c for c in matches if c.relation == "finish_start"
                and c.min_lag_minutes == c.max_lag_minutes == -hint.offset_minutes]
        else:
            matches = [c for c in matches if c.relation == "during" and c.min_lag_minutes == 0]
        if len(matches) != 1:
            raise ValueError("source_parallel_window_not_preserved")


def _orders(count, constraints, durations):
    parents, children = [set() for _ in range(count)], [set() for _ in range(count)]
    for c in constraints:
        if c.predecessor >= count or c.successor >= count or c.predecessor == c.successor:
            raise ValueError("invalid_temporal_reference")
        if c.max_lag_minutes is not None and c.max_lag_minutes < c.min_lag_minutes:
            raise ValueError("reversed_temporal_window")
        parents[c.successor].add(c.predecessor)
        children[c.predecessor].add(c.successor)
    todo, order = set(range(count)), []
    while todo:
        ready = sorted(i for i in todo if parents[i].issubset(order))
        if not ready:
            raise ValueError("cyclic_reviewed_dependencies")
        order.extend(ready)
        todo.difference_update(ready)
    critical = list(durations)
    for i in reversed(order):
        critical[i] += max((critical[child] for child in children[i]), default=0.)
    return parents, critical


def _validate_placements(placements, durations, resources, constraints):
    if len(placements) != len(durations) or {p.action_id for p in placements} != set(range(len(durations))):
        raise ValueError("schedule_action_coverage_mismatch")
    by_id = {p.action_id: p for p in placements}
    for p in placements:
        if p.end_minutes <= p.start_minutes or not math.isclose(p.end_minutes - p.start_minutes, durations[p.action_id], rel_tol=0., abs_tol=EPS):
            raise ValueError("schedule_changed_source_duration")
    calendars = {}
    for p in placements:
        for resource in resources[p.action_id]:
            calendars.setdefault(resource, []).append(p)
    for intervals in calendars.values():
        intervals.sort(key=lambda p: p.start_minutes)
        if any(a.end_minutes > b.start_minutes + EPS for a, b in zip(intervals, intervals[1:])):
            raise ValueError("schedule_resource_collision")
    for c in constraints:
        parent, child = by_id[c.predecessor], by_id[c.successor]
        origin = parent.end_minutes if c.relation == "finish_start" else parent.start_minutes
        lag = child.start_minutes - origin
        if lag < c.min_lag_minutes - EPS or (c.max_lag_minutes is not None and lag > c.max_lag_minutes + EPS):
            raise ValueError("schedule_temporal_constraint_violated")
        if c.relation == "during" and child.start_minutes >= parent.end_minutes - EPS:
            raise ValueError("schedule_missed_existing_overlap")


def _greedy(durations, resources, constraints, parents, critical):
    remaining, placed, tails = set(range(len(durations))), {}, {}
    while remaining:
        ready = [i for i in remaining if parents[i].issubset(placed)]
        i = min(ready, key=lambda key: (-critical[key], key))
        start = max((tails.get(resource, 0.) for resource in resources[i]), default=0.)
        for c in constraints:
            if c.successor == i:
                parent = placed[c.predecessor]
                origin = parent.end_minutes if c.relation == "finish_start" else parent.start_minutes
                start = max(start, origin + c.min_lag_minutes)
        start = max(0., start)
        placed[i] = Placement(action_id=i, start_minutes=start, end_minutes=start + durations[i])
        for resource in resources[i]:
            tails[resource] = placed[i].end_minutes
        remaining.remove(i)
    result = list(placed.values())
    _validate_placements(result, durations, resources, constraints)
    return result


def schedule_meal(recipe: dict, understanding: TimeUnderstanding, review: ScheduleReview | None = None) -> MealSchedule:
    """Schedule exact reviewed durations; unknown times/ranges never become invented totals."""
    result = MealSchedule(original_instructions=list(recipe["instructions"]))
    if (not 1 <= len(recipe["instructions"]) <= 30 or sum(map(len, recipe["instructions"])) > 12000
            or not 1 <= len(understanding.actions) <= 80):
        result.reasons = ["bounded_source_action_plan_required"]
        return result
    understanding = validate_understanding(recipe, understanding)
    facts = propose_execution_facts(recipe, understanding)
    diagnostics = list(facts.warnings) + [f"action:{action.action_id}:{warning}" for action in facts.actions for warning in action.warnings
        if warning != "resource_occupancy_requires_review"]
    if review is None:
        result.reasons = ["execution_fact_review_required"] + diagnostics
        return result
    try:
        review = ScheduleReview.model_validate(review.model_dump())
        if (review.context_hash != facts.context_hash or review.understanding_hash != facts.understanding_hash
                or review.facts_hash != facts.facts_hash):
            raise ValueError("stale_execution_review")
        if not review.confirmed or not review.reviewed_by:
            result.reasons = ["execution_fact_review_required"] + diagnostics
            return result
        if diagnostics:
            result.reasons = diagnostics
            return result
        if len(review.actions) != len(understanding.actions) or {a.action_id for a in review.actions} != set(range(len(understanding.actions))):
            raise ValueError("review_action_coverage_mismatch")
        by_id = {a.action_id: a for a in review.actions}
        resources, durations = [], []
        constraints = list(review.constraints)
        for action, proposal in zip(understanding.actions, facts.actions):
            confirmed = by_id[action.action_id]
            if not confirmed.timing_scope_confirmed:
                raise ValueError(f"whole_action_duration_scope_unconfirmed:action:{action.action_id}")
            use = resource_keys(confirmed.resources)
            if not {r.resource for r in proposal.resources}.issubset(use):
                raise ValueError(f"source_resource_omitted:action:{action.action_id}")
            if action.duration_minutes is None or action.duration_minutes.lower <= 0:
                raise ValueError("positive_complete_source_durations_required")
            if action.attention == "passive" and not confirmed.completion_confirmed:
                raise ValueError(f"passive_completion_or_handoff_unconfirmed:action:{action.action_id}")
            if confirmed.handoff_action_id is not None:
                handoff = confirmed.handoff_action_id
                if handoff >= len(understanding.actions) or understanding.actions[handoff].attention != "active":
                    raise ValueError("handoff_requires_a_valid_active_action")
                constraints.append(TemporalConstraint(predecessor=action.action_id, successor=handoff,
                    min_lag_minutes=0., max_lag_minutes=0.))
            resources.append(use)
            durations.append(action.duration_minutes.upper)
        constraints.extend(_state_constraints(review))
        _source_windows(facts, constraints)
        parents, critical = _orders(len(durations), constraints, durations)
        baseline = None
        if review.baseline_mode == "reviewed_serial":
            if understanding.existing_parallelism or facts.time_hints:
                raise ValueError("serial_baseline_would_erase_existing_parallelism")
            baseline, cursor = [], 0.
            for i, duration in enumerate(durations):
                baseline.append(Placement(action_id=i, start_minutes=cursor, end_minutes=cursor + duration))
                cursor += duration
        elif review.baseline_mode == "provided":
            baseline = review.baseline
        if baseline is not None:
            _validate_placements(baseline, durations, resources, constraints)
        try:
            proposed = _greedy(durations, resources, constraints, parents, critical)
        except ValueError:
            if baseline is None:
                raise ValueError("heuristic_found_no_feasible_schedule_not_a_proof_of_impossibility")
            proposed = baseline
            result.reasons.append("heuristic_did_not_improve_valid_baseline")
        def export(placements):
            return [ScheduledAction(**p.model_dump(), step_id=understanding.actions[p.action_id].step_id,
                source_text=understanding.actions[p.action_id].text, resources=resources[p.action_id])
                for p in sorted(placements, key=lambda p: (p.start_minutes, p.action_id))]
        result.proposed = export(proposed)
        result.proposed_minutes = max(p.end_minutes for p in proposed)
        result.review_hash = digest(review.model_dump(mode="json"))
        result.assumptions = ["Exact source timers cover all action scope, including setup, as confirmed by review.",
            "One cook and unit-capacity equipment; passive completion/handoffs and physical states are reviewed.",
            "Greedy non-preemptive scheduling is bounded and is not an optimality or cooking-safety proof."]
        if baseline is None:
            result.reasons.append("baseline_not_established_no_saving_claim")
            return result
        baseline_end = max(p.end_minutes for p in baseline)
        if result.proposed_minutes >= baseline_end - EPS:
            result.proposed, result.proposed_minutes = export(baseline), baseline_end
        result.baseline, result.baseline_minutes = export(baseline), baseline_end
        result.estimated_saving_minutes = max(0., baseline_end - result.proposed_minutes)
        result.status = "improved_under_reviewed_assumptions" if result.estimated_saving_minutes > EPS else "no_verified_improvement"
        if review.baseline_mode == "reviewed_serial":
            result.assumptions.append("Baseline is a separately reviewed serial-workflow assumption, not a measured original cook time.")
        return result
    except ValueError as error:
        result.reasons = [str(error)]
        return result
