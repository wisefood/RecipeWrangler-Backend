"""Production approval policy using the actual scoring helper and comparisons."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_adaptation_preview import inputs, composition
from test_adaptation_candidate_selection import catalogue
from test_adaptation_enhancement import breakdown
from recipe_wrangler.services.adaptation import enhancement_screening as screening
from recipe_wrangler.services.adaptation.candidate_selection import shortlist_raw_part_candidates
from recipe_wrangler.services.adaptation.objective_approval import InputScoreContext, approve_nutrition_candidate, _fvl_member
from recipe_wrangler.services.adaptation.models import NutrientObjective
from recipe_wrangler.services.adaptation.nutrition_benefit import DEFAULT_POLICY, STRICT_POLICY, WITHIN_GRADE_POLICY


def context(snapshot):
    return InputScoreContext(recipe_id=snapshot.recipe_id, baseline_run_id=snapshot.run_id,
        quantity_input_hash=snapshot.quantity_input_hash, total_weight_g=200., food_type="solid",
        fruit_veg_legume_percent=0., evidence={"reference": "review", "method": "fixture_review",
            "version": "1", "status": "stated"})


@pytest.mark.parametrize('identity,expected', [
    ('cheese', False), ('butter', False), ('rice', False), ('chicken', False),
    ('soy sauce', False), ('olive oil', True), ('rapeseed oil', True),
    ('sunflower oil', False), ('carrot', True), ('chickpea', True),
    ('almond', True), ('pepper', None),
])
def test_fvl_member_uses_production_food_groups_without_guessing_ambiguous_foods(identity, expected):
    composition = SimpleNamespace(properties={'base_identity': SimpleNamespace(value=identity,
        evidence=SimpleNamespace(status='stated'))})
    assert _fvl_member(composition) is expected


def comparison(inputs, catalogue):
    catalogue(inputs[1])
    return shortlist_raw_part_candidates(inputs[0], target_position=0,
        objective=NutrientObjective(metric="energy_kcal")).screening[0].comparison


def test_default_also_accepts_supported_strict_grade_improvement(inputs, catalogue):
    inputs[0].ingredients[0].composition.values_per_100g["energy_kcal"] = 600
    checked = comparison(inputs, catalogue)
    before = deepcopy(checked.model_dump())
    result = approve_nutrition_candidate(inputs[0], checked, NutrientObjective(metric="energy_kcal"), context(inputs[0]))
    assert result["status"] == "approved_input_objective"
    assert result["target_points_saved"] > 0
    assert result["before"]["nutri_score"] > result["after"]["nutri_score"]
    assert result["basis"] == "raw_edible_input" and not result["finished_recipe_score"]
    assert checked.model_dump() == before


@pytest.mark.parametrize('metric', ['carbs_g', 'fat_g'])
def test_incompatible_changed_definition_cannot_pass_on_numerical_totals_alone(inputs, catalogue, metric):
    from recipe_wrangler.services.adaptation.comparison import compare_raw_part
    checked = comparison(inputs, catalogue)
    checked.target_composition.nutrient_definitions[metric] = 'incompatible analytical definition'
    checked = compare_raw_part(inputs[0], checked.edit, checked.target_composition)
    assert checked.before['complete_totals'][metric] is not None
    assert checked.after['complete_totals'][metric] is not None
    assert checked.recipe_delta_per_serving[metric] is None
    result = approve_nutrition_candidate(inputs[0], checked, NutrientObjective(metric='energy_kcal'), context(inputs[0]))
    assert result['status'] == 'needs_evidence'
    assert result['reasons'] == ['complete_comparable_guard_nutrients_required']


def test_unknown_carbohydrate_definition_is_disclosed_without_blocking_a_supported_energy_goal(inputs, catalogue):
    from recipe_wrangler.services.adaptation.comparison import compare_raw_part
    checked = comparison(inputs, catalogue)
    inputs[0].ingredients[0].composition.nutrient_definitions.pop('carbs_g')
    checked.target_composition.nutrient_definitions.pop('carbs_g')
    checked = compare_raw_part(inputs[0], checked.edit, checked.target_composition)
    result = approve_nutrition_candidate(inputs[0], checked, NutrientObjective(metric='energy_kcal'), context(inputs[0]))
    assert result['status'] == 'approved_input_objective'
    assert result['unassessed_nutrients'] == ['carbs_g']
    assert result['nutrient_deltas_per_serving']['carbs_g'] is None
    assert checked.after['complete_totals']['carbs_g'] is None


def test_explicit_strict_policy_rejects_unchanged_letter_grade(inputs, catalogue):
    checked = comparison(inputs, catalogue)
    result = approve_nutrition_candidate(inputs[0], checked, NutrientObjective(metric="energy_kcal"), context(inputs[0]),
        nutrition_policy=STRICT_POLICY)
    assert result["status"] == "rejected"
    assert "no_strict_letter_grade_improvement" in result["reasons"]


def test_same_comparable_inputs_pass_separate_within_grade_policy(inputs,catalogue):
    checked=comparison(inputs,catalogue)
    result=approve_nutrition_candidate(inputs[0],checked,NutrientObjective(metric="energy_kcal"),context(inputs[0]),
        nutrition_policy=WITHIN_GRADE_POLICY)
    assert result["status"]=="approved_input_objective",result
    assert result["before"]["nutri_score"]==result["after"]["nutri_score"]
    assert result["target_benefit"]["reduction_per_serving"]>=50
    assert result["version"]=="nutrition_objective_approval_within_grade_pilot_v1"


@pytest.mark.parametrize("issue,reason", [
    ("scope", "score_context_baseline_mismatch"),
    ("mass", "score_context_mass_mismatch"),
    ("evidence", "stated_score_context_required"),
    ("comparison", "comparison_changed"),
    ("objective", "unsupported_production_nutrition_objective"),
])
@pytest.mark.parametrize("policy",[DEFAULT_POLICY,STRICT_POLICY,WITHIN_GRADE_POLICY])
def test_invalid_assumptions_or_changed_comparison_never_get_approval(inputs, catalogue, issue, reason,policy):
    checked = comparison(inputs, catalogue)
    score_context = context(inputs[0])
    objective = NutrientObjective(metric="energy_kcal")
    if issue == "scope": score_context.baseline_run_id = "other"
    elif issue == "mass": score_context.total_weight_g = 500
    elif issue == "evidence": score_context.evidence.status = "inferred"
    elif issue == "comparison": checked.after["complete_totals"]["sugar_g"] = 99
    else: objective.metric = "protein_g"
    result = approve_nutrition_candidate(inputs[0], checked, objective, score_context,nutrition_policy=policy)
    assert result["status"] != "approved_input_objective" and result["reasons"] == [reason]


def test_missing_score_nutrient_blocks_score_but_keeps_nutrient_comparison(inputs, catalogue):
    inputs[1][0].values_per_100g["fibre_g"] = None
    checked = comparison(inputs, catalogue)
    assert checked.recipe_delta_per_serving["energy_kcal"] < 0
    result = approve_nutrition_candidate(inputs[0], checked, NutrientObjective(metric="energy_kcal"), context(inputs[0]))
    assert result["status"] == "needs_evidence"
    assert result["reasons"] == ["complete_comparable_score_nutrients_required"]


def test_final_policy_failures_cannot_fill_shortlist_and_hide_good_candidate(inputs, catalogue):
    inputs[0].ingredients[0].composition.values_per_100g["energy_kcal"] = 600
    bad = [composition(f"bad-{index}", "breast", 350) for index in range(11)]
    for item in bad:
        item.values_per_100g.update(sugar_g=30, saturated_fat_g=15, sodium_mg=1000)
    good = composition("good", "breast", 400)
    catalogue(bad + [good])
    result = screening.screen_enhancement(inputs[0], mode="nutrition",
        nutri_score_breakdown=breakdown(energy=7), score_context=context(inputs[0]))
    assert result["objective_approval"] == "pending"
    assert [item["candidate_id"] for item in result["candidate_refs"]] == ["good"]
    candidates = result["attempts"][0]["shortlist"]["screening"]
    assert all(item["status"] == "rejected" and item["objective_preflight"]["status"] == "rejected"
        for item in candidates[:11])


def test_nutrient_improving_candidates_reach_context_before_final_grade_decision(inputs, catalogue):
    catalogue(inputs[1])
    result = screening.screen_enhancement(inputs[0], mode="nutrition",
        nutri_score_breakdown=breakdown(energy=5))
    assert result["status"] == "candidates_screened" and result["candidate_refs"]
    assert result["objective_approval"] == "pending"
