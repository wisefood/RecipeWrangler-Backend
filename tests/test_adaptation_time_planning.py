"""One-cook overlap checks; source order alone never approves a faster plan."""
import pytest

from adaptation_reference_fixtures import time_snapshot
from recipe_wrangler.services.adaptation import time_planning as planner
from recipe_wrangler.services.adaptation import time_understanding as temporal


def context(instructions=None, attention=None, parents=None):
    snapshot = time_snapshot()
    recipe = dict(recipe_id=snapshot.recipe_id, title="Baked potatoes and dressing",
        originals=["potatoes", "dressing"], instructions=instructions or [
            "Bake the potatoes for 30 minutes.", "Whisk the dressing for 7 minutes."])
    fragments = temporal.source_fragments(recipe)
    extraction = temporal.ActionExtraction(existing_parallelism=False, uncertainties=[], actions=[
            dict(fragment_id=i, attention=attention[i] if attention else ("passive" if i == 0 else "active"),
                depends_on=parents[i] if parents else []) for i in range(len(fragments))])
    return snapshot, recipe, temporal._materialize_actions(recipe, extraction)


def review(candidate, **changes):
    data = dict(candidate_id=candidate.candidate_id, context_hash=candidate.context_hash,
        understanding_hash=candidate.understanding_hash, independent_and_ready=True,
        unattended_window_without_other_cook_tasks=True, anchor_resources={"oven"},
        complete_work_timing_confirmed=True,
        work_resources={"cook", "bowl"}, reviewed_by="synthetic source review")
    return planner.OverlapReview(**{**data, **changes})


def test_discovery_finds_source_window_but_cannot_approve_it():
    _, recipe, understanding = context()
    result = planner.discover_overlaps(recipe, understanding)
    assert result.status == "candidates_require_review" and len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.anchor_step_id == "step:0" and candidate.work_step_id == "step:1"
    assert candidate.window_minutes.lower == 30 and candidate.work_minutes.upper == 7
    assert candidate.review_required
    assessment = planner.assess_overlap(recipe, understanding, candidate.candidate_id)
    assert assessment.status == "needs_evidence" and assessment.slack_minutes is None


def test_reviewed_work_fits_and_renders_one_exact_unsaved_draft(monkeypatch):
    snapshot, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    def forbidden(*args, **kwargs):
        pytest.fail("Discovery, fit checks and rendering must be offline")
    monkeypatch.setattr(temporal, "_config", forbidden)
    monkeypatch.setattr(temporal, "_request", forbidden)
    result = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.assessment.status == "feasible_for_draft"
    assert result.assessment.slack_minutes.lower == 23
    assert not result.assessment.recipe_ready
    assert result.draft.status == "draft_requires_review" and not result.draft.recipe_ready
    assert result.draft.candidate_id == candidate.candidate_id
    assert result.draft.model is None and result.draft.comparison is None
    assert len(result.draft.patches) == 1
    assert result.draft.instructions[0].text == recipe["instructions"][0]
    assert result.draft.instructions[1].text == "While the potatoes bake, whisk the dressing for 7 minutes."
    assert [i.quantity for i in result.draft.ingredients] == [i.quantity for i in snapshot.ingredients]


@pytest.mark.parametrize("window,work,status", [
    ("20-30", "5-7", "feasible_for_draft"),
    ("30", "40", "not_applicable"),
    ("30", "25-35", "needs_evidence"),
    ("30", "0", "not_applicable"),
])
def test_worst_case_range_fit_and_impossible_work(window, work, status):
    _, recipe, understanding = context([
        f"Bake the potatoes for {window} minutes.", f"Whisk the dressing for {work} minutes."])
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.status == status
    if status == "feasible_for_draft":
        assert result.slack_minutes.lower == 13 and result.slack_minutes.upper == 25
    else:
        assert result.slack_minutes is None


@pytest.mark.parametrize("window,work,attention", [
    ("Bake for about 30 minutes.", "Whisk for 7 minutes.", ["passive", "active"]),
    ("Bake for 30 minutes until golden.", "Whisk for 7 minutes.", ["passive", "active"]),
    ("Bake for 30 minutes.", "Chop the salad.", ["passive", "active"]),
    ("Bake for 30 minutes.", "Whisk for 7 minutes.", ["passive", "unknown"]),
    ("Bake for 30 minutes.", "Whisk for 7 minutes.", ["passive", "mixed"]),
    ("Bake for 30 minutes.", "Rest the dressing for 7 minutes.", ["passive", "passive"]),
])
def test_unresolved_timings_or_attention_cannot_produce_a_draft(window, work, attention):
    snapshot, recipe, understanding = context([window, work], attention=attention)
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.assessment.status == "needs_evidence" and result.draft is None


@pytest.mark.parametrize("anchor,work", [
    ({"oven"}, {"cook", "oven"}),
    ({"OVEN:main"}, {"cook", "oven:upper"}),
    ({"oven", "COOK"}, {"cook", "bowl"}),
    ({"hob"}, {"cook", "bowl"}),
    ({"oven"}, {"bowl"}),
])
def test_shared_equipment_or_cook_conflicts_are_rejected(anchor, work):
    _, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id,
        review(candidate, anchor_resources=anchor, work_resources=work))
    assert result.status == "not_applicable" and result.slack_minutes is None


@pytest.mark.parametrize("field", ["candidate_id", "context_hash", "understanding_hash"])
def test_reviews_are_bound_to_recipe_inference_and_candidate(field):
    _, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id,
        review(candidate, **{field: "stale"}))
    assert result.status == "needs_evidence" and result.reasons == ["review_stale_or_for_another_candidate"]


def test_changed_inference_invalidates_candidate():
    _, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    understanding.uncertainties.append("new source review needed")
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.status == "needs_evidence" and result.candidate is None


@pytest.mark.parametrize("parents", [[[], [0]], [[], [0], [1]]])
def test_direct_and_transitive_dependencies_prevent_overlap(parents):
    instructions = ["Bake for 30 minutes.", "Whisk for 7 minutes."]
    if len(parents) == 3:
        instructions.append("Chop for 2 minutes.")
    _, recipe, understanding = context(instructions, parents=parents)
    result = planner.discover_overlaps(recipe, understanding)
    assert result.status == "not_applicable" and not result.candidates


@pytest.mark.parametrize("text", [
    "After baking, whisk for 7 minutes.", "Meanwhile whisk for 7 minutes.",
    "When the oven finishes, whisk for 7 minutes.", "Serve the potatoes.",
])
def test_explicit_time_and_serving_constraints_are_not_rewritten(text):
    _, recipe, understanding = context(["Bake for 30 minutes.", text])
    assert planner.discover_overlaps(recipe, understanding).status == "not_applicable"


def test_existing_parallelism_requires_a_baseline_instead_of_claiming_savings():
    _, recipe, understanding = context()
    understanding.existing_parallelism = True
    result = planner.discover_overlaps(recipe, understanding)
    assert result.status == "needs_evidence" and not result.candidates
    assert result.reasons == ["existing_parallelism_requires_baseline_analysis"]


def test_nonadjacent_work_references_the_correct_baking_step():
    snapshot, recipe, understanding = context([
        "Bake for 30 minutes.", "Slice the baked potatoes for 2 minutes.", "Whisk for 7 minutes."],
        parents=[[], [0], []])
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    assert candidate.work_step_id == "step:2"
    result = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.draft.instructions[2].text.startswith("After starting the baking stage in step 1,")
    assert result.draft.instructions[1].text == recipe["instructions"][1]


@pytest.mark.parametrize("case", ["no_actions", "too_many_actions", "too_many_steps", "active_only"])
def test_unsupported_scope_returns_a_reason(case):
    _, recipe, understanding = context()
    if case == "no_actions":
        understanding.actions = []
    elif case == "too_many_actions":
        understanding.actions = understanding.actions * 41
    elif case == "too_many_steps":
        recipe["instructions"] = ["Whisk."] * 31
    else:
        _, recipe, understanding = context(["Stir for 30 minutes.", "Whisk for 7 minutes."], attention=["active", "active"])
    result = planner.discover_overlaps(recipe, understanding)
    assert result.status in {"needs_evidence", "not_applicable"} and not result.candidates and result.reasons


def test_stale_snapshot_cannot_render_after_fit_check():
    snapshot, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    snapshot.recipe_id = "another-recipe"
    result = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.assessment.status == "needs_evidence"
    assert result.assessment.slack_minutes is None and result.draft is None


@pytest.mark.parametrize("tamper", ["independence", "attention", "resources"])
def test_mutated_or_malformed_review_cannot_bypass_validation(tamper):
    _, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    facts = review(candidate)
    if tamper == "independence":
        facts.independent_and_ready = False
    elif tamper == "attention":
        facts.unattended_window_without_other_cook_tasks = False
    else:
        facts.anchor_resources = {":"}
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id, facts)
    assert result.status == "needs_evidence" and result.slack_minutes is None


def test_candidate_enumeration_is_bounded_and_reports_truncation():
    _, recipe, understanding = context(["Bake for 30 minutes."] + ["Whisk for 2 minutes."] * 29)
    result = planner.discover_overlaps(recipe, understanding)
    assert len(result.candidates) == 20 and result.reasons == ["candidate_limit_reached"]
    assert all(candidate.review_required for candidate in result.candidates)


def test_one_timer_does_not_prove_complete_prep_duration():
    snapshot, recipe, understanding = context([
        "Bake for 30 minutes.", "Get the bowl, whisk the dressing for 7 minutes."])
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id,
        review(candidate, complete_work_timing_confirmed=False))
    assert result.assessment.status == "needs_evidence" and result.draft is None
    assert "whole_work_duration_scope_not_confirmed" in result.assessment.reasons


def test_complete_scope_confirmation_cannot_supply_missing_source_time():
    _, recipe, understanding = context(["Bake for 30 minutes.", "Chop the salad."])
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id, review(candidate))
    assert result.status == "needs_evidence" and "complete_work_duration_not_bounded" in result.reasons


def test_code_only_understanding_finds_simmer_overlap_and_links_shared_foods():
    recipe = dict(recipe_id="r", title="Stew", originals=["beef", "salad"], instructions=[
        "Simmer the beef for 40 minutes.", "Cut the cucumber for 5 minutes.", "Stir the beef."])
    understanding = temporal.deterministic_understanding(recipe)
    assert [a.attention for a in understanding.actions] == ["passive", "active", "active"]
    assert understanding.actions[2].depends_on == [0]
    result = planner.discover_overlaps(recipe, understanding)
    assert [c.work_step_id for c in result.candidates] == ["step:1"]
    assert result.candidates[0].window_minutes.upper == 40


def test_code_only_understanding_does_not_overlap_a_step_that_reuses_the_anchor_food():
    recipe = dict(recipe_id="r", title="Stew", originals=["beef"], instructions=[
        "Simmer the beef for 40 minutes.", "Slice the beef for 5 minutes."])
    result = planner.discover_overlaps(recipe, temporal.deterministic_understanding(recipe))
    assert result.status == "not_applicable"


def test_start_only_draft_for_a_simmer_anchor_claims_no_minutes():
    recipe = dict(recipe_id="r", title="Pasta", originals=["pasta", "tomato"], instructions=[
        "Boil the pasta for about 15 minutes until softened.", "Chop the tomato for a few minutes."])
    understanding = temporal.deterministic_understanding(recipe)
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    snapshot = time_snapshot()
    snapshot = snapshot.model_copy(update={"recipe_id": "r"})
    rev = review(candidate, anchor_resources={"hob", "pot"}, complete_work_timing_confirmed=False)
    assert planner.assess_overlap(recipe, understanding, candidate.candidate_id, rev).status == "needs_evidence"
    plan = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, rev, start_only=True)
    assert plan.assessment.status == "feasible_for_draft" and plan.assessment.slack_minutes is None
    assert plan.draft.instructions[1].text == "While the pasta boils, chop the tomato for a few minutes."


def test_bake_anchor_still_requires_an_oven_in_start_only_mode():
    _, recipe, understanding = context()
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    result = planner.assess_overlap(recipe, understanding, candidate.candidate_id,
        review(candidate, anchor_resources={"hob"}), start_only=True)
    assert result.status == "not_applicable"



def test_wording_falls_back_to_the_stage_prefix_when_the_anchor_names_no_single_food():
    _, recipe, understanding = context(["Bake the potatoes and dressing for 30 minutes.", "Whisk the sauce for 7 minutes."])
    snapshot = time_snapshot()
    snapshot = snapshot.model_copy(update={"recipe_id": recipe["recipe_id"]})
    candidate = planner.discover_overlaps(recipe, understanding).candidates[0]
    plan = planner.plan_reviewed_overlap(snapshot, recipe, understanding, candidate.candidate_id, review(candidate))
    assert plan.draft.instructions[1].text == "Once the preceding baking stage has started, begin this step: Whisk the sauce for 7 minutes."
