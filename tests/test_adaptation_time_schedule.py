"""Whole-meal estimates require complete, separately reviewed execution facts."""
import pytest

from recipe_wrangler.services.adaptation import time_understanding as temporal
from recipe_wrangler.services.adaptation.execution_facts import StateValue, propose_execution_facts
from recipe_wrangler.services.adaptation.time_schedule import (
    Placement, TemporalConstraint, make_schedule_review, schedule_meal,
)


def context(instructions=None, attention=None, parents=None, parallel=False, originals=None):
    recipe = dict(recipe_id="schedule-fixture", title="Synthetic complete-timing example",
        originals=originals or ["bread", "dressing", "onions"], instructions=instructions or [
            "Bake bread for 30 minutes.", "Whisk dressing for 7 minutes.", "Chop onions for 5 minutes."])
    fragments = temporal.source_fragments(recipe)
    labels = temporal.ActionExtraction(existing_parallelism=parallel, uncertainties=[], actions=[dict(
            fragment_id=i, attention=attention[i] if attention else ("passive" if i == 0 else "active"),
            depends_on=parents[i] if parents else []) for i in range(len(fragments))])
    understanding = temporal._materialize_actions(recipe, labels)
    facts = propose_execution_facts(recipe, understanding)
    review = make_schedule_review(facts, understanding)
    # Synthetic review explicitly assumes timers include all setup and safe completion.
    review.confirmed, review.reviewed_by, review.baseline_mode = True, "synthetic test reviewer", "reviewed_serial"
    for action in review.actions:
        action.timing_scope_confirmed = action.completion_confirmed = True
    return recipe, understanding, facts, review


def test_independent_work_overlaps_baking_but_one_cook_tasks_do_not_overlap(monkeypatch):
    recipe, understanding, facts, review = context()
    monkeypatch.setattr(temporal, "_request", lambda *a, **kw: pytest.fail("offline only"))
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "improved_under_reviewed_assumptions"
    assert (result.baseline_minutes, result.proposed_minutes, result.estimated_saving_minutes) == (42, 30, 12)
    by_id = {a.action_id: a for a in result.proposed}
    assert by_id[1].end_minutes <= by_id[2].start_minutes
    assert result.original_instructions == recipe["instructions"] and not result.recipe_ready
    assert all(a.source_text == understanding.actions[a.action_id].text for a in result.proposed)
    assert facts.review_required and result.review_hash


@pytest.mark.parametrize("instructions,attention,total", [
    (["Bake bread for 30 minutes.", "Bake potatoes for 20 minutes."], ["passive", "passive"], 50),
    (["Whisk dressing for 7 minutes.", "Chop onions for 5 minutes."], ["active", "active"], 12),
])
def test_exclusive_resources_prevent_impossible_suggestions(instructions, attention, total):
    recipe, understanding, _, review = context(instructions, attention)
    # Instance labels cannot manufacture multiple ovens or cooks.
    for action in review.actions:
        action.resources = {name.upper() + ":main" for name in action.resources}
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "no_verified_improvement"
    assert result.proposed_minutes == total and result.estimated_saving_minutes == 0


def test_state_production_enforces_dependency_even_if_model_edge_is_missing():
    recipe, understanding, facts, review = context([
        "Mix dough for 5 minutes.", "Bake mixed dough for 30 minutes.", "Whisk dressing for 7 minutes."],
        ["active", "passive", "active"])
    assert any(s.item == "dough" and s.state == "mixed" and s.kind == "required" for s in facts.actions[1].states)
    assert review.constraints == []
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "improved_under_reviewed_assumptions"
    by_id = {a.action_id: a for a in result.proposed}
    assert by_id[1].start_minutes >= by_id[0].end_minutes
    assert result.proposed_minutes == 35 and result.estimated_saving_minutes == 7


def test_required_state_cannot_be_assumed_without_reviewed_initial_or_produced_state():
    recipe, understanding, facts, review = context(["Mix cooked beans for 5 minutes."], ["active"], originals=["cooked beans"])
    assert facts.initial_state_mentions[0].quote == "cooked beans"
    assert schedule_meal(recipe, understanding, review).status == "no_verified_improvement"
    review.initial_states = []
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "needs_evidence" and "required_physical_state_unavailable" in result.reasons[0]


def test_fact_proposals_are_source_quoted_and_never_confirm_review():
    recipe, understanding, facts, _ = context(["Bake mixed dough in the oven for 30 minutes.",
        "Whisk dressing in a bowl for 7 minutes."])
    for fact, action in zip(facts.actions, understanding.actions):
        assert all(r.quote in action.text for r in fact.resources)
        assert all(s.quote in action.text for s in fact.states)
    assert any(r.resource == "oven" and r.basis == "stated_mention" for r in facts.actions[0].resources)
    review = make_schedule_review(facts, understanding)
    assert not review.confirmed and not any(a.timing_scope_confirmed for a in review.actions)
    assert schedule_meal(recipe, understanding, review).status == "needs_evidence"


@pytest.mark.parametrize("problem", ["unconfirmed", "reviewer", "context", "inference", "facts", "coverage", "scope", "resource", "completion"])
def test_incomplete_or_stale_review_has_no_saving(problem):
    recipe, understanding, _, review = context()
    if problem == "unconfirmed": review.confirmed = False
    if problem == "reviewer": review.reviewed_by = None
    if problem == "context": review.context_hash = "stale"
    if problem == "inference": review.understanding_hash = "stale"
    if problem == "facts": review.facts_hash = "stale"
    if problem == "coverage": review.actions.pop()
    if problem == "scope": review.actions[1].timing_scope_confirmed = False
    if problem == "resource": review.actions[0].resources.clear()
    if problem == "completion": review.actions[0].completion_confirmed = False
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "needs_evidence" and result.estimated_saving_minutes is None


@pytest.mark.parametrize("instruction,attention", [
    ("Bake bread for 20-30 minutes.", "passive"), ("Bake bread for about 30 minutes.", "passive"),
    ("Bake bread until golden.", "passive"), ("Chop onions.", "active"),
    ("Whisk dressing for 7 minutes.", "unknown"), ("Whisk dressing for 7 minutes.", "mixed"),
])
def test_incomplete_timings_never_become_numeric_meal_estimates(instruction, attention):
    recipe, understanding, _, review = context([instruction], [attention])
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "needs_evidence" and result.proposed_minutes is None
    assert result.estimated_saving_minutes is None


def parallel_context(offset=False):
    work = "10 min before the end, whisk dressing for 5 minutes." if offset else "Meanwhile whisk dressing for 7 minutes."
    recipe, understanding, facts, review = context(["Bake bread for 30 minutes.", work,
        "Chop onions for 10 minutes."], parallel=True)
    hint = facts.time_hints[0]
    review.constraints = [TemporalConstraint(predecessor=0, successor=1,
        relation="finish_start" if offset else "during", min_lag_minutes=-10. if offset else 0.,
        max_lag_minutes=-10. if offset else None, source_action_id=1, quote=hint.quote)]
    review.baseline_mode = "provided"
    review.baseline = [Placement(action_id=0, start_minutes=0., end_minutes=30.),
        Placement(action_id=1, start_minutes=20. if offset else 0., end_minutes=25. if offset else 7.),
        Placement(action_id=2, start_minutes=30., end_minutes=40.)]
    return recipe, understanding, facts, review


@pytest.mark.parametrize("offset", [False, True])
def test_existing_overlap_and_before_end_offset_are_preserved(offset):
    recipe, understanding, facts, review = parallel_context(offset)
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "improved_under_reviewed_assumptions" and result.estimated_saving_minutes == 10
    work = next(p for p in result.proposed if p.action_id == 1)
    if offset:
        assert work.start_minutes == 20
    else:
        assert 0 <= work.start_minutes < 30
    assert understanding.actions[1].duration_minutes.upper == (5 if offset else 7)
    review.baseline = [Placement(**p.model_dump(include={"action_id", "start_minutes", "end_minutes"})) for p in result.proposed]
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "no_verified_improvement" and result.estimated_saving_minutes == 0


def test_existing_overlap_cannot_be_erased_to_inflate_saving():
    recipe, understanding, _, review = parallel_context()
    review.baseline_mode = "reviewed_serial"
    assert schedule_meal(recipe, understanding, review).reasons == ["serial_baseline_would_erase_existing_parallelism"]
    review.baseline_mode, review.constraints = "provided", []
    assert schedule_meal(recipe, understanding, review).reasons == ["source_parallel_window_not_preserved"]


@pytest.mark.parametrize("work,expected", [("7", "improved_under_reviewed_assumptions"), ("20", "no_verified_improvement")])
def test_exact_passive_completion_handoff_cannot_be_delayed_by_busy_cook(work, expected):
    recipe, understanding, _, review = context(["Bake bread for 10 minutes.",
        f"Whisk dressing for {work} minutes.", "Transfer bread for 1 minute."], ["passive", "active", "active"])
    review.actions[0].handoff_action_id = 2
    review.baseline_mode = "provided"
    review.baseline = [Placement(action_id=0, start_minutes=0., end_minutes=10.),
        Placement(action_id=2, start_minutes=10., end_minutes=11.),
        Placement(action_id=1, start_minutes=11., end_minutes=11. + float(work))]
    result = schedule_meal(recipe, understanding, review)
    assert result.status == expected
    assert next(a for a in result.proposed if a.action_id == 2).start_minutes == 10


@pytest.mark.parametrize("problem", ["collision", "duration", "missing", "dependency", "cycle", "reference"])
def test_invalid_baseline_or_dependencies_are_not_used(problem):
    recipe, understanding, _, review = context()
    if problem == "cycle":
        review.constraints = [TemporalConstraint(predecessor=0, successor=1), TemporalConstraint(predecessor=1, successor=0)]
    elif problem == "reference": review.constraints = [TemporalConstraint(predecessor=100, successor=0)]
    else:
        review.baseline_mode = "provided"
        review.baseline = [Placement(action_id=0, start_minutes=0., end_minutes=30.),
            Placement(action_id=1, start_minutes=30., end_minutes=37.),
            Placement(action_id=2, start_minutes=37., end_minutes=42.)]
        if problem == "collision":
            review.baseline[2].start_minutes, review.baseline[2].end_minutes = 31., 36.
        if problem == "duration": review.baseline[0].end_minutes = 31.
        if problem == "missing": review.baseline.pop()
        if problem == "dependency": review.constraints = [TemporalConstraint(predecessor=2, successor=1)]
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "needs_evidence" and result.estimated_saving_minutes is None


def test_unknown_baseline_allows_only_reviewable_timeline_without_a_saving_claim():
    recipe, understanding, _, review = context()
    review.baseline_mode = "unknown"
    result = schedule_meal(recipe, understanding, review)
    assert result.status == "needs_evidence" and result.proposed_minutes == 30
    assert result.estimated_saving_minutes is None and not result.baseline


def test_conflicting_reviewed_initial_states_are_rejected():
    recipe, understanding, _, review = context()
    review.initial_states = [StateValue(item="rice", state="raw"), StateValue(item="RICE", state="cooked")]
    assert schedule_meal(recipe, understanding, review).reasons == ["conflicting_initial_states"]


@pytest.mark.parametrize("text", ["Refrigerate leftovers within 2 hours.", "10 min before the end, whisk dressing."])
def test_deadlines_and_offsets_are_not_action_durations(text):
    recipe, understanding, _, review = context([text], ["active"])
    assert understanding.actions[0].duration_minutes is None
    assert schedule_meal(recipe, understanding, review).estimated_saving_minutes is None


def test_state_cues_do_not_treat_outcome_adverbs_as_food_objects():
    _, _, facts, _ = context(["Check chicken is cooked through.", "When cool enough to handle, shred chicken."],
        ["active", "active"])
    assert all(not action.states for action in facts.actions)
