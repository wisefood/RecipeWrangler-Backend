from unittest.mock import patch

from recipe_wrangler.services.adaptation import adaptation_row, time_live

RECIPE = {
    "title": "Roast potatoes and dressing",
    "original_ingredients": ["500 g potatoes", "2 tbsp olive oil", "1 lemon"],
    "instructions": ["1. Roast the potatoes in the oven for 40 minutes.", "2. Mix the olive oil and lemon in a bowl.", "3. Serve."],
}


def run(recipe=RECIPE, **kw):
    with patch.object(time_live, "fetch_recipe_info_by_id", return_value=recipe):
        return time_live.adapt_active_recipe("r1", "ie", **kw)


def test_overlap_row_has_stored_row_shape_and_claims_no_minutes():
    row = run()
    assert row["status"] == "adapted" and row["mode"] == "time" and row["region"] == "IE" and row["source"] == "live"
    assert row["adapted"]["changed_steps"] == [1] and row["adapted"]["steps"][0] == row["original"]["steps"][0]
    assert row["adapted"]["steps"][1] != row["original"]["steps"][1] and "roast" in row["adapted"]["steps"][1].lower()
    assert row["benefit"]["metric"] is None and row["provenance"]["flags"] == {"unreviewed": True, "start_only": True}


def test_no_passive_step_is_no_suggestion():
    row = run(dict(RECIPE, instructions=["1. Chop the potatoes.", "2. Mix with the oil."]))
    assert row["status"] == "no_suggestion" and row["reason_code"] == "no_overlap_found"


def test_no_instructions_is_not_scored():
    assert run(dict(RECIPE, instructions=[]))["reason_code"] == "no_instructions"


def test_recipe_breaking_user_diet_is_not_offered():
    row = run(dict(RECIPE, original_ingredients=["500 g beef", "1 lemon"]), diet="vegan")
    assert row["status"] == "not_suitable_for_user" and row["reason_code"] == "recipe_breaks_user_restriction"


def test_mode_time_goes_through_adaptation_row_and_gets_a_reason():
    with patch("recipe_wrangler.repositories.recipe_adaptations.get_adaptation", return_value=None), \
         patch.object(time_live, "fetch_recipe_info_by_id", return_value=dict(RECIPE, instructions=["1. Chop."])):
        row = adaptation_row.adaptation_row("r1", "time", "IE")
    assert row["status"] == "no_suggestion" and "passive step" in row["reason"]


def test_model_estimate_gives_clipped_minutes_saved():
    row = run(llm=lambda m, n, t: '{"minutes_low": 5, "minutes_high": 90}')
    assert row["benefit"]["metric"] == {"name": "minutes_saved", "unit": "min", "lower": 5, "upper": 40}
    assert row["provenance"]["flags"]["indicative"] and row["provenance"]["llm"]["status"] == "ok"


def test_bad_model_answer_keeps_start_only_row():
    for bad in ('not json', '{"minutes_low": 9, "minutes_high": 3}', '{"minutes_low": 0, "minutes_high": 500}'):
        row = run(llm=lambda m, n, t, bad=bad: bad)
        assert row["status"] == "adapted" and row["benefit"]["metric"] is None and row["provenance"]["llm"]["status"] == "estimate_failed"


def test_only_independent_prep_steps_are_moved():
    for work in ("Serve with the dressing.", "Finish the sauce by stirring in the herbs.", "Preheat the oven to 160C.", "To prepare the filling, heat oil in a pan."):
        row = run(dict(RECIPE, instructions=["1. Roast the potatoes in the oven for 40 minutes.", f"2. {work}", "3. Enjoy."]))
        assert row["status"] == "no_suggestion", work
    assert run(dict(RECIPE, instructions=["1. Roast the potatoes for 2 minutes.", "2. Mix the lemon and oil.", "3. Enjoy."]))["status"] == "no_suggestion"
