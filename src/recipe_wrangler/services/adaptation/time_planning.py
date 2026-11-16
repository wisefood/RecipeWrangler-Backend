"""Discover passive-step overlaps and check one reviewed window; never infer a meal saving."""
from typing import Literal
import re

from pydantic import Field

from recipe_wrangler.repositories.matching_store import digest
from .models import ComparisonModel, NumberRange, Text
from .models import InstructionDraft
from .time_understanding import (
    ActionIndex, PASSIVE_ANCHOR, ReviewedOverlap, TimeUnderstanding, resource_keys,
    rewrite_reviewed_overlap, text_hash, validate_understanding,
)


class OverlapCandidate(ComparisonModel):
    candidate_id: Text
    context_hash: Text
    understanding_hash: Text
    anchor_action_id: ActionIndex
    anchor_step_id: Text
    work_step_id: Text
    anchor_quote: str
    window_minutes: NumberRange
    work_minutes: NumberRange | None
    timing_reasons: list[Text]
    review_required: Literal[True] = True


class OverlapDiscovery(ComparisonModel):
    status: Literal["candidates_require_review", "needs_evidence", "not_applicable"]
    candidates: list[OverlapCandidate] = Field(default_factory=list)
    reasons: list[Text] = Field(default_factory=list)


class OverlapReview(ComparisonModel):
    """Trusted caller's source review, never filled by the model or discovery."""
    candidate_id: Text
    context_hash: Text
    understanding_hash: Text
    independent_and_ready: Literal[True]
    unattended_window_without_other_cook_tasks: Literal[True]
    complete_work_timing_confirmed: bool = Field(default=False, strict=True)
    anchor_resources: set[Text] = Field(min_length=1)
    work_resources: set[Text] = Field(min_length=1)
    reviewed_by: Text


class OverlapAssessment(ComparisonModel):
    status: Literal["feasible_for_draft", "needs_evidence", "not_applicable"]
    candidate: OverlapCandidate | None = None
    reasons: list[Text] = Field(default_factory=list)
    slack_minutes: NumberRange | None = None
    recipe_ready: Literal[False] = False


class OverlapPlan(ComparisonModel):
    assessment: OverlapAssessment
    draft: InstructionDraft | None = None


def _ancestors(actions, index):
    found, pending = set(), list(actions[index].depends_on)
    while pending:
        parent = pending.pop()
        if parent not in found:
            found.add(parent)
            pending.extend(actions[parent].depends_on)
    return found


def _timing_reasons(anchor, work):
    reasons = []
    qualified = r"\b(?:about|approximately|around|until|at least|up to)\b"
    if re.search(qualified, anchor.text, re.I):
        reasons.append("baking_window_has_approximate_or_outcome_timing")
    if any(action.attention != "active" for action in work):
        reasons.append("work_requires_phase_or_attention_review")
    if any(action.duration_minutes is None or re.search(qualified, action.text, re.I) for action in work):
        reasons.append("complete_work_duration_not_bounded")
    return reasons


def discover_overlaps(recipe: dict, understanding: TimeUnderstanding) -> OverlapDiscovery:
    """Find candidates only; source order and absent model edges do not prove independence."""
    if (not 1 <= len(recipe["instructions"]) <= 30 or sum(map(len, recipe["instructions"])) > 12000
            or len(understanding.actions) > 80):
        return OverlapDiscovery(status="needs_evidence", reasons=["recipe_exceeds_planning_bounds"])
    understanding = validate_understanding(recipe, understanding)
    actions = understanding.actions
    if not actions:
        return OverlapDiscovery(status="needs_evidence", reasons=["source_action_records_required"])
    if understanding.existing_parallelism:
        return OverlapDiscovery(status="needs_evidence", reasons=["existing_parallelism_requires_baseline_analysis"])
    context_hash = text_hash(recipe)
    understanding_hash = digest(understanding.model_dump(mode="json"))
    candidates = []
    for anchor in actions:
        if (anchor.attention != "passive" or anchor.duration_minutes is None
                or anchor.duration_minutes.lower <= 0 or not PASSIVE_ANCHOR.search(anchor.text)):
            continue
        anchor_index = int(anchor.step_id.split(":")[1])
        for step_index in range(anchor_index + 1, len(recipe["instructions"])):
            text = recipe["instructions"][step_index]
            # Preserve temporal/serving instructions instead of contradicting them with a prefix.
            if re.search(r"\b(?:while|meanwhile|before|after|once|when|during|serve|serving)\b", text, re.I):
                continue
            work_step = f"step:{step_index}"
            work = [action for action in actions if action.step_id == work_step]
            work_ids = {action.action_id for action in work}
            if any(any(parent >= anchor.action_id and parent not in work_ids
                       for parent in _ancestors(actions, action.action_id)) for action in work):
                continue
            reasons = _timing_reasons(anchor, work)
            minutes = None
            if "complete_work_duration_not_bounded" not in reasons and "work_requires_phase_or_attention_review" not in reasons:
                minutes = NumberRange(lower=sum(a.duration_minutes.lower for a in work),
                    upper=sum(a.duration_minutes.upper for a in work))
            key = dict(context_hash=context_hash, understanding_hash=understanding_hash,
                anchor_action_id=anchor.action_id, anchor_step_id=anchor.step_id, work_step_id=work_step)
            candidates.append(OverlapCandidate(candidate_id=digest(key), **key,
                anchor_quote=anchor.text, window_minutes=anchor.duration_minutes,
                work_minutes=minutes, timing_reasons=reasons))
            if len(candidates) == 20:
                return OverlapDiscovery(status="candidates_require_review", candidates=candidates,
                    reasons=["candidate_limit_reached"])
    return OverlapDiscovery(status="candidates_require_review" if candidates else "not_applicable",
        candidates=candidates, reasons=[] if candidates else ["no_supported_independent_baking_overlap"])


def assess_overlap(recipe: dict, understanding: TimeUnderstanding, candidate_id: str,
                   review: OverlapReview | None = None, start_only: bool = False) -> OverlapAssessment:
    """Check a whole prep step fits inside one reviewed unattended window.

    start_only: the draft only says to start the work during the passive step; no fit, slack or minutes are claimed,
    so unknown or qualified timings do not block it (independence and resources still do).
    """
    discovery = discover_overlaps(recipe, understanding)
    candidate = next((item for item in discovery.candidates if item.candidate_id == candidate_id), None)
    result = OverlapAssessment(status="needs_evidence", candidate=candidate)
    if candidate is None:
        result.reasons = discovery.reasons or ["candidate_stale_or_unknown"]
        return result
    if review is None:
        result.reasons = ["independence_attention_and_resource_review_required"]
        return result
    try:
        review = OverlapReview.model_validate(review.model_dump())
    except ValueError:
        result.reasons = ["review_invalid_or_unconfirmed"]
        return result
    if (review.candidate_id != candidate.candidate_id or review.context_hash != candidate.context_hash
            or review.understanding_hash != candidate.understanding_hash):
        result.reasons = ["review_stale_or_for_another_candidate"]
        return result
    try:
        anchor_resources = resource_keys(review.anchor_resources)
        work_resources = resource_keys(review.work_resources)
    except ValueError:
        result.reasons = ["canonical_resource_names_required"]
        return result
    oven_needed = re.search(r"\b(?:bak\w*|roast\w*)\b", candidate.anchor_quote, re.I)
    if ("cook" in anchor_resources or (oven_needed and "oven" not in anchor_resources) or "cook" not in work_resources
            or anchor_resources & work_resources):
        result.status, result.reasons = "not_applicable", ["reviewed_resources_or_attention_conflict"]
        return result
    if start_only:
        result.status, result.reasons = "feasible_for_draft", ["start_only_no_time_saving_claim"]
        return result
    timing_reasons = list(candidate.timing_reasons)
    if not review.complete_work_timing_confirmed:
        timing_reasons.append("whole_work_duration_scope_not_confirmed")
    if timing_reasons or candidate.work_minutes is None:
        result.reasons = timing_reasons or ["complete_work_duration_not_bounded"]
        return result
    work, window = candidate.work_minutes, candidate.window_minutes
    if work.lower <= 0:
        result.status, result.reasons = "not_applicable", ["no_positive_work_duration"]
    elif work.lower > window.upper:
        result.status, result.reasons = "not_applicable", ["work_cannot_fit_baking_window"]
    elif work.upper > window.lower:
        result.reasons = ["duration_ranges_do_not_guarantee_fit"]
    else:
        result.status = "feasible_for_draft"
        result.slack_minutes = NumberRange(lower=window.lower - work.upper, upper=window.upper - work.lower)
    return result


def plan_reviewed_overlap(snapshot, recipe: dict, understanding: TimeUnderstanding,
                          candidate_id: str, review: OverlapReview | None = None, start_only: bool = False) -> OverlapPlan:
    assessment = assess_overlap(recipe, understanding, candidate_id, review, start_only)
    plan = OverlapPlan(assessment=assessment)
    if assessment.status != "feasible_for_draft":
        return plan
    candidate = assessment.candidate
    draft = rewrite_reviewed_overlap(snapshot, recipe, understanding, ReviewedOverlap(
        context_hash=candidate.context_hash, anchor_step_id=candidate.anchor_step_id,
        work_step_id=candidate.work_step_id, passive_anchor_quote=candidate.anchor_quote,
        anchor_resources=review.anchor_resources, work_resources=review.work_resources,
        independent_of_anchor_output=True, start_only_no_completion_deadline_claim=True,
        reviewed_by=review.reviewed_by))
    if draft.status != "draft_requires_review":
        assessment.status, assessment.reasons = "needs_evidence", draft.reasons
        assessment.slack_minutes = None
        return plan
    draft.candidate_id = candidate.candidate_id
    plan.draft = draft
    return plan
