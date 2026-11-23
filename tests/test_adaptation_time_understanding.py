"""Temporal model outputs remain inferred; only reviewed exact overlap wording can execute."""
import json

import pytest

from adaptation_reference_fixtures import time_snapshot
from recipe_wrangler.services.adaptation import time_understanding as service


@pytest.fixture
def context(monkeypatch):
    snapshot = time_snapshot()
    recipe = {"recipe_id": snapshot.recipe_id, "title": "Example", "originals": ["cake", "dressing"],
        "instructions": ["Bake the cake for 30 minutes. Cool the cake.", "Whisk the dressing ingredients."]}
    understanding = service.TimeUnderstanding(steps=[
        {"step_id": "step:0", "evidence_quote": "Bake the cake", "attention": "mixed", "depends_on": [], "duration_quote": "30 minutes", "duration_minutes": {"lower": 30., "upper": 30.}},
        {"step_id": "step:1", "evidence_quote": "Whisk the dressing", "attention": "active", "depends_on": [], "duration_quote": None, "duration_minutes": None}],
        existing_parallelism=False, uncertainties=[])
    review = service.ReviewedOverlap(context_hash=service.text_hash(recipe), anchor_step_id="step:0", work_step_id="step:1",
        passive_anchor_quote="Bake the cake for 30 minutes.", anchor_resources={"oven"}, work_resources={"cook"},
        independent_of_anchor_output=True, start_only_no_completion_deadline_claim=True, reviewed_by="synthetic-test")
    monkeypatch.setattr(service, "_config", lambda: {"model": "qwen/qwen3-32b", "source": "openrouter"})
    return snapshot, recipe, understanding, review


def test_exact_rewrite_preserves_original_steps_and_quantities_without_model(context, monkeypatch):
    snapshot, recipe, understanding, review = context
    def forbidden(*args, **kwargs):
        pytest.fail("Reviewed text rendering needs neither model configuration nor a model call")
    monkeypatch.setattr(service, "_request", forbidden)
    monkeypatch.setattr(service, "_config", forbidden)
    result = service.rewrite_reviewed_overlap(snapshot, recipe, understanding, review)
    assert result.status == "draft_requires_review"
    assert not result.recipe_ready
    assert result.model is None
    assert result.instructions[0].text == recipe["instructions"][0]
    assert result.instructions[1].text == "While the cake bakes, whisk the dressing ingredients."
    assert [i.quantity for i in result.ingredients] == [i.quantity for i in snapshot.ingredients]


@pytest.mark.parametrize("tamper", ["quote", "duration", "cycle", "missing_step"])
def test_invalid_extraction_is_rejected(context, tamper):
    _, recipe, understanding, _ = context
    if tamper == "quote":
        understanding.steps[0].evidence_quote = "invented"
    elif tamper == "duration":
        understanding.steps[0].duration_minutes.lower = 15.
    elif tamper == "cycle":
        understanding.steps[0].depends_on = ["step:1"]
        understanding.steps[1].depends_on = ["step:0"]
    else:
        understanding.steps.pop()
    with pytest.raises(ValueError):
        service.validate_understanding(recipe, understanding)


@pytest.mark.parametrize("tamper", ["stale", "resources", "attention", "dependency"])
def test_unsafe_overlap_makes_no_model_request(context, tamper):
    snapshot, recipe, understanding, review = context
    if tamper == "stale":
        review.context_hash = "old"
    elif tamper == "resources":
        review.anchor_resources = {"cook"}
    elif tamper == "attention":
        understanding.steps[0].attention = "active"
    else:
        understanding.steps[1].depends_on = ["step:0"]
    assert service.rewrite_reviewed_overlap(snapshot, recipe, understanding, review).status == "needs_evidence"


def test_review_cannot_supply_arbitrary_rewrite_text(context):
    _, _, _, review = context
    with pytest.raises(ValueError):
        service.ReviewedOverlap.model_validate({**review.model_dump(), "after": "Bake for 10 minutes instead."})


def test_extractor_receives_explicit_ids(context):
    _, recipe, understanding, _ = context
    def request(payload, *args, **kwargs):
        assert payload["fragments"][0]["fragment_id"] == 0
        assert payload["fragments"][0]["step_id"] == "step:0"
        return service.ActionExtraction(**understanding.model_dump(exclude={"steps", "actions"}), actions=[
            dict(fragment_id=0, attention="passive", depends_on=[]),
            dict(fragment_id=1, attention="passive", depends_on=[0]),
            dict(fragment_id=2, attention="active", depends_on=[]),
        ]).model_dump_json()
    assert service.extract_time_understanding(recipe, request=request).steps[0].step_id == "step:0"


def test_mixed_timer_is_not_reported_as_entire_step_duration(context):
    _, recipe, understanding, _ = context
    recipe["instructions"][0] += " Rest for 5 minutes."
    with pytest.raises(ValueError, match="mixed_step_duration_unresolved"):
        service.validate_understanding(recipe, understanding)


def test_local_model_fallback_is_prohibited(monkeypatch):
    monkeypatch.setattr(service, "_llm_config", lambda: {"source": "vllm", "base_url": "http://localhost:8005/v1"})
    with pytest.raises(ValueError, match="no_local_fallback"):
        service._config()


def test_schema_echo_is_rejected_instead_of_used_as_recipe_data(context):
    _, recipe, _, _ = context
    def echo_schema(payload, response_model, config, **kwargs):
        return json.dumps(response_model.model_json_schema())
    with pytest.raises(ValueError):
        service.extract_time_understanding(recipe, request=echo_schema)


@pytest.fixture
def phased_recipe(context):
    _, recipe, understanding, _ = context
    recipe["instructions"] = ["Knead for 10 minutes. Leave to rise for 1 hour.", "Bake for 30 minutes. Transfer to a rack to cool."]
    output = service.ActionExtraction(**understanding.model_dump(exclude={"steps", "actions"}), actions=[
        dict(fragment_id=0, attention="active", depends_on=[]),
        dict(fragment_id=1, attention="passive", depends_on=[0]),
        dict(fragment_id=2, attention="passive", depends_on=[1]),
        dict(fragment_id=3, attention="active", depends_on=[2]),
    ])
    return recipe, output


def test_mixed_actions_keep_every_source_character_and_each_timer(phased_recipe):
    recipe, output = phased_recipe
    result = service.validate_understanding(recipe, service._materialize_actions(recipe, output))
    assert [action.duration_minutes.lower if action.duration_minutes else None for action in result.actions] == [10, 60, 30, None]
    assert [step.attention for step in result.steps] == ["mixed", "mixed"]
    assert all(step.duration_minutes is None for step in result.steps)
    assert result.steps[1].depends_on == ["step:0"]
    for i, text in enumerate(recipe["instructions"]):
        assert "".join(a.text for a in result.actions if a.step_id == f"step:{i}") == text


@pytest.mark.parametrize("tamper", ["missing_tail", "invented_id", "duplicate_id", "cycle"])
def test_invalid_action_phases_are_rejected(phased_recipe, tamper):
    recipe, output = phased_recipe
    if tamper == "missing_tail":
        output.actions.pop()
    elif tamper == "invented_id":
        output.actions[0].fragment_id = 999
    elif tamper == "duplicate_id":
        output.actions[1].fragment_id = 0
    else:
        output.actions[0].depends_on = [1]
    with pytest.raises(ValueError):
        service._materialize_actions(recipe, output)


def test_offsets_are_not_cooking_durations():
    assert service._action_duration("About 30 min before the end, cook rice according to the pack.") == (None, None)


def test_unresolved_multi_timer_fragment_is_rejected():
    with pytest.raises(ValueError, match="action_contains_multiple_timers"):
        service._action_duration("Cook for 10 minutes plus 5 minutes.")


def test_conjunction_split_preserves_source_and_separates_timers(phased_recipe):
    recipe, output = phased_recipe
    recipe["instructions"][0] = "Knead for 10 minutes and leave to rise for 1 hour.\n"
    result = service._materialize_actions(recipe, output)
    assert "".join(a.text for a in result.actions if a.step_id == "step:0") == recipe["instructions"][0]
    assert [a.duration_minutes.lower for a in result.actions[:2]] == [10, 60]


@pytest.mark.parametrize("tamper", ["text", "timer", "summary"])
def test_persisted_action_data_cannot_change_source_or_summary(phased_recipe, tamper):
    recipe, output = phased_recipe
    result = service._materialize_actions(recipe, output)
    if tamper == "text":
        result.actions[0].text = "Bake for 1 minute."
    elif tamper == "timer":
        result.actions[0].duration_minutes.lower = 1
    else:
        result.steps[0].attention = "passive"
    with pytest.raises(ValueError):
        service.validate_understanding(recipe, result)


def test_unresolved_phase_does_not_gain_a_validated_duration(phased_recipe):
    recipe, output = phased_recipe
    output.actions[0].attention = "unknown"
    result = service.validate_understanding(recipe, service._materialize_actions(recipe, output))
    assert result.actions[0].duration_quote is None
    assert result.actions[0].duration_minutes is None
    assert result.steps[0].attention == "unknown"


def test_unconfirmed_review_cannot_render(context):
    snapshot, recipe, understanding, review = context
    review.independent_of_anchor_output = False
    result = service.rewrite_reviewed_overlap(snapshot, recipe, understanding, review)
    assert result.status == "needs_evidence" and result.reasons == ["invalid_review_record"]


def test_conflicting_model_attention_stays_unknown_not_approved(phased_recipe):
    recipe, output = phased_recipe
    output.actions[0].attention = "passive"
    result = service.validate_understanding(recipe, service._materialize_actions(recipe, output))
    assert result.actions[0].attention == "unknown"
    assert result.actions[0].duration_minutes is None
    assert any("Rejected passive label for action 0" in text for text in result.uncertainties)
    result.actions[0].attention = "passive"
    with pytest.raises(ValueError, match="passive_action_contains_handling"):
        service.validate_understanding(recipe, result)
