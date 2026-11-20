import json
from unittest.mock import patch

from recipe_wrangler.services.adaptation import adaptation_row, cost_live, cost_llm, service

from test_cost_adaptation import _FakeCatalogue, _fake_substitutes

PROFILE = {
    "title": "Beef stew",
    "nutri_score_breakdown": {"nutri_score": "Nutriscore_B"},
    "nutrition_profiling_details": [
        {"name": "beef", "weight_g": 300.0},
        {"name": "salt", "weight_g": 5.0},
    ],
}
RECIPE = {
    "title": "Beef stew",
    "ingredients": [
        {"name": "salt", "measurement": "1 teaspoon"},
        {"name": "beef", "measurement": "300.0 g"},
    ],
    "instructions": ["1. Brown the beef.", "2. Simmer."],
}


def run(allergens=(), diet=None, recipe=RECIPE, subs=_fake_substitutes, llm=None):
    with (
        patch.object(service, "_load_profile", return_value=PROFILE),
        patch.object(service, "_serves_from_row", return_value=2.0),
        patch.object(service, "_authoritative_grade", return_value="B"),
        patch.object(service, "load_cost_catalogue", return_value=_FakeCatalogue()),
        patch.object(service, "find_substitute_candidates", side_effect=subs),
        patch.object(service, "resolve_graph_name", side_effect=lambda name, hints: name),
        patch.object(service, "_select_graph_name", side_effect=lambda raw, hints: raw),
        patch.object(service, "get_ingredient_allergens", return_value=[]),
        patch.object(service, "flavor_similarity", return_value=0.2),
        patch.object(service, "_food_class_compatible", return_value=True),
        patch.object(cost_live, "fetch_recipe_info_by_id", return_value=recipe),
        # the matcher must not run: cost needs only name and weight
        patch.object(service, "nutritional_tool_vector", side_effect=AssertionError("matcher called")),
        patch.object(service, "_fetch_candidate_profile", side_effect=AssertionError("nutri-guard ran")),
    ):
        return cost_live.adapt_active_recipe("r1", "IE", allergens=allergens, diet=diet, llm=llm)


def test_adapted_row_has_stored_row_shape_without_the_matcher():
    row = run()
    assert row["status"] == "adapted" and row["is_adapted"] and row["mode"] == "cost" and row["source"] == "live"
    assert row["adapted"]["lines"] == ["1 teaspoon salt", "300.0 g beef  →  use pork"]
    assert row["adapted"]["changed_positions"] == [1]
    assert row["swap"]["candidate_label"] == "pork"
    m = row["benefit"]["metric"]
    assert (m["before"], m["after"]) == (1.5, 0.9) and m["unit"] == "EUR per serving"


def test_recipe_that_breaks_user_diet_is_not_suitable():
    row = run(diet="vegetarian")
    assert row["status"] == "not_suitable_for_user" and row["reason_code"] == "recipe_breaks_user_restriction"


def test_user_restrictions_reach_the_candidate_gate():
    seen = []

    def gate(parts, lines, allergens=(), diet=None):
        seen.append((tuple(allergens), diet))
        return ["pork: blocked"] if allergens else []

    with patch("recipe_wrangler.services.adaptation.nutrition_adaptation.restriction_problems", side_effect=gate):
        row = run(allergens=["gluten"])
    assert (("gluten",), None) in seen
    assert row["status"] == "no_suggestion" and row["reason_code"] == "alternatives_add_allergen"


def test_no_swap_without_restriction_stays_no_cheaper_option():
    row = run(subs=lambda g: [])
    assert row["status"] == "no_suggestion" and row["reason_code"] == "no_cheaper_option"


def test_cost_mode_is_routed_live():
    assert adaptation_row.MODES["cost"][0] is not None


LLM_RECIPE = dict(RECIPE, original_ingredients=["300g beef, diced", "1 teaspoon salt"])


def fake_llm(choice=1, title="Pork stew", step="Brown the pork.", line="300g pork, diced"):
    def llm(messages, schema, max_tokens):
        if schema == "ChoiceOut":
            return json.dumps({"choice": choice, "ranking": [choice] if choice else [], "reason": "fits the dish"})
        user = json.loads(messages[-1]["content"].split("\n\nYour previous")[0])
        return json.dumps({"title": title, "instructions": [{"source_steps": [1], "text": step}, {"source_steps": [2], "text": "Simmer."}],
                           "ingredient_lines": [{"position": c["position"], "new": line} for c in user["changed_ingredient_lines_input"]], "notes": []})
    return llm


def test_llm_choice_and_rewrite_make_the_text():
    row = run(recipe=LLM_RECIPE, llm=fake_llm())
    assert row["status"] == "adapted" and row["provenance"]["llm"]["status"] == "ok"
    assert row["adapted"]["title"] == "Pork stew" and row["adapted"]["steps"] == ["Brown the pork.", "Simmer."]
    assert row["adapted"]["lines"] == ["300g pork, diced", "1 teaspoon salt"] and row["adapted"]["changed_positions"] == [0]
    assert row["benefit"]["tip_source"] == "code" and "37%" not in row["benefit"]["tip"] and "40%" in row["benefit"]["tip"]


def test_llm_says_no_candidate_keeps_the_code_card():
    row = run(recipe=LLM_RECIPE, llm=fake_llm(choice=None))
    assert row["status"] == "adapted" and row["provenance"]["llm"]["status"] == "no_culinary_candidate"
    assert row["adapted"]["lines"][0] == "300g beef, diced  →  use pork" and row["adapted"]["title"] == "Beef stew"


def test_rewrite_that_changes_the_amount_is_rejected_and_falls_back():
    row = run(recipe=LLM_RECIPE, llm=fake_llm(line="500g pork, diced"))
    assert row["provenance"]["llm"]["status"] == "rewrite_failed" and row["adapted"]["lines"][0].endswith("→  use pork")


def test_rewrite_that_adds_a_user_allergen_is_rejected():
    row = run(recipe=LLM_RECIPE, llm=fake_llm(line="300g pork and wheat noodles"), allergens=["gluten"])
    assert row["provenance"]["llm"]["status"] in ("rewrite_failed", "restriction_failed")


def test_client_is_off_by_default_and_follows_the_adaptation_llm_setting(monkeypatch):
    monkeypatch.delenv("COST_ADAPT_LLM", raising=False)
    assert cost_llm.client() is None
    monkeypatch.setenv("COST_ADAPT_LLM", "true")
    monkeypatch.setenv("ADAPT_LLM_SOURCE", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert callable(cost_llm.client())
