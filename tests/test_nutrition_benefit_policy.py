"""Development benefit thresholds and guards, independent of model opinions."""
from copy import deepcopy

import pytest

from recipe_wrangler.services.adaptation.nutrition_benefit import (
    MIN_REDUCTION, ROUNDING_TOLERANCE, SCORE_KEYS, DEFAULT_POLICY, STRICT_POLICY, WITHIN_GRADE_POLICY, LEGACY_MEANINGFUL_POLICY,
    assess_nutrition_benefit,
)


def score(grade="A"):
    # Controlled score inputs for gate boundary tests; joined tests recompute real scores.
    return {"nutri_score":"Nutriscore_"+grade,
        "negative_points":{"items":{key:{"points":0} for key in SCORE_KEYS.values()}}}


def check(before,after,metric="saturated_fat_g",**kwargs):
    return assess_nutrition_benefit(before,after,metric,score(),kwargs.pop("after_score",score()),
        policy=kwargs.pop("policy",DEFAULT_POLICY),**kwargs)


def values(**updates):
    return dict(dict.fromkeys(ROUNDING_TOLERANCE,10.0),**updates)


@pytest.mark.parametrize("metric",MIN_REDUCTION)
def test_requires_both_absolute_and_relative_target_benefits(metric):
    minimum=MIN_REDUCTION[metric]
    before=values(**{metric:minimum*10})
    after={**before,metric:before[metric]-minimum}
    assert check(before,after,metric)["status"]=="approved_input_objective"
    assert "target_reduction_below_absolute_minimum" in check(before,{**after,metric:after[metric]+0.01},metric)["reasons"]
    before[metric]=minimum*20
    after[metric]=before[metric]-minimum
    assert "target_reduction_below_relative_minimum" in check(before,after,metric)["reasons"]


@pytest.mark.parametrize("metric",[m for m in ROUNDING_TOLERANCE if m!="saturated_fat_g"])
def test_other_nutrient_regressions_are_not_excused_by_target_benefit(metric):
    before=values()
    sign=-1 if metric in {"fibre_g","protein_g"} else 1
    after={**before,"saturated_fat_g":8.0,metric:before[metric]+sign*ROUNDING_TOLERANCE[metric]*2}
    assert "nutrient_regression:"+metric in check(before,after,policy=LEGACY_MEANINGFUL_POLICY)["reasons"]


def test_rounding_suppression_does_not_relax_benefit_threshold_or_protein_tradeoff():
    before=values()
    after={**before,"saturated_fat_g":8.,"energy_kcal":10.0000044}
    result=check(before,after,policy=LEGACY_MEANINGFUL_POLICY)
    assert result["status"]=="approved_input_objective"
    after["protein_g"]-=0.225
    assert check(before,after,policy=LEGACY_MEANINGFUL_POLICY)["reasons"]==["nutrient_regression:protein_g"]
    assert check(before,after)['status']=='approved_input_objective'
    assert check(before,after)['accepted_tradeoffs'][0]['metric']=='protein_g'
    assert check(before,{**before,"saturated_fat_g":9.99999})["status"]=="rejected"


@pytest.mark.parametrize("metric",[m for m in ROUNDING_TOLERANCE if m!="saturated_fat_g"])
def test_exact_rounding_boundary_survives_binary_float_representation(metric):
    before=values(**{metric:20.})
    sign=-1 if metric in {"fibre_g","protein_g"} else 1
    after={**before,"saturated_fat_g":8.,metric:before[metric]+sign*ROUNDING_TOLERANCE[metric]}
    assert check(before,after)["status"]=="approved_input_objective"


@pytest.mark.parametrize("missing",[None,float("nan"),float("inf"),True,-0.1])
@pytest.mark.parametrize("side",["before","after"])
def test_missing_or_invalid_nutrients_never_become_zero(side,missing):
    before=values();after={**before,"saturated_fat_g":8.}
    (before if side=="before" else after)["sodium_mg"]=missing
    result=check(before,after)
    assert result["status"]=="needs_evidence"
    assert result["reasons"]==["complete_comparable_guard_nutrients_required"]


def test_no_change_zero_baseline_grade_regression_and_invalid_policy():
    before=values()
    assert check(before,before)["status"]=="rejected"
    assert check(values(saturated_fat_g=0),values(saturated_fat_g=0))["status"]=="rejected"
    assert "nutrition_grade_regression" in check(before,{**before,"saturated_fat_g":8.},after_score=score("B"))["reasons"]
    assert check(before,{**before,"saturated_fat_g":8.},after_score=score("?"))["status"]=="blocked"
    with pytest.raises(ValueError,match="Unknown nutrition benefit policy"):
        check(before,before,policy="unknown")


def test_explicit_strict_policy_keeps_grade_points_and_target_threshold_rules_without_mutation():
    before=values();after={**before,"saturated_fat_g":8.}
    original=deepcopy((before,after))
    result=assess_nutrition_benefit(before,after,"saturated_fat_g",score(),score(),policy=STRICT_POLICY)
    assert result["benefit_policy"]==STRICT_POLICY
    assert result["reasons"]==["target_below_production_threshold_on_comparable_inputs",
        "no_target_negative_point_improvement","no_strict_letter_grade_improvement"]
    assert (before,after)==original


def test_default_accepts_meaningful_a_to_a_benefit_without_point_improvement():
    before=values();after={**before,"saturated_fat_g":8.}
    original=deepcopy((before,after))
    result=assess_nutrition_benefit(before,after,"saturated_fat_g",score(),score())
    assert result["benefit_policy"]==DEFAULT_POLICY
    assert result["status"]=="approved_input_objective"
    assert result["target_points_saved"]==0
    assert result["target_benefit"]["reduction_per_serving"]==2
    assert (before,after)==original


def test_default_reuses_frozen_within_grade_numerical_thresholds():
    before=values();after={**before,"saturated_fat_g":8.}
    current=check(before,after,policy=LEGACY_MEANINGFUL_POLICY)
    pilot=check(before,after,policy=WITHIN_GRADE_POLICY)
    assert {k:v for k,v in current.items() if k!="benefit_policy"}=={
        k:v for k,v in pilot.items() if k!="benefit_policy"}


@pytest.mark.parametrize('metric', [m for m in SCORE_KEYS if m != 'saturated_fat_g'] + ['protein_g','fibre_g'])
def test_prototype_allows_disclosed_five_percent_tradeoffs_but_not_larger_losses(metric):
    before=values()
    sign=-1 if metric in {'protein_g','fibre_g'} else 1
    after={**before,'saturated_fat_g':8.,metric:before[metric]*(1+sign*.05)}
    result=check(before,after)
    assert result['status']=='approved_input_objective'
    assert result['accepted_tradeoffs'][0]['metric']==metric
    after[metric]+=sign*.001
    assert 'nutrient_regression:'+metric in check(before,after)['reasons']


def test_zero_baseline_does_not_allow_an_unbounded_new_nutrient():
    before=values(sodium_mg=0.)
    assert 'nutrient_regression:sodium_mg' in check(before,{**before,'saturated_fat_g':8.,'sodium_mg':1.})['reasons']


def test_prototype_reports_carbs_and_total_fat_without_treating_increases_as_automatically_bad():
    before=values()
    result=check(before,{**before,'saturated_fat_g':8.,'carbs_g':12.,'fat_g':11.})
    assert result['status']=='approved_input_objective'
    assert result['nutrient_deltas_per_serving']['carbs_g']==2.
    assert result['non_guard_nutrients']==['carbs_g','fat_g']
