"""Numerical gates on comparable inputs; these never establish recipe suitability."""

import math
from typing import Literal

NutritionPolicy = Literal["strict_grade_v2", "within_grade_pilot_v1", "meaningful_nutrient_v1", "prototype_nutrient_v1"]
STRICT_POLICY = "strict_grade_v2"
WITHIN_GRADE_POLICY = "within_grade_pilot_v1"
LEGACY_MEANINGFUL_POLICY = "meaningful_nutrient_v1"
DEFAULT_POLICY = "prototype_nutrient_v1"
MAX_OTHER_NUTRIENT_REGRESSION = 0.05
MIN_TARGET_POINTS = 3
SCORE_KEYS = {"energy_kcal":"energy", "sugar_g":"sugar", "saturated_fat_g":"saturated_fats", "sodium_mg":"sodium"}
SCORE_METRICS = (*SCORE_KEYS, "fibre_g", "protein_g")
# Provisional engineering thresholds, not clinical or regulatory claims.
MIN_REDUCTION = {"energy_kcal":50.0, "sugar_g":5.0, "saturated_fat_g":1.0, "sodium_mg":100.0}
MIN_RELATIVE_REDUCTION = 0.10
ROUNDING_TOLERANCE = {"energy_kcal":0.1, "protein_g":0.001, "carbs_g":0.001, "fat_g":0.001,
                      "sugar_g":0.001, "saturated_fat_g":0.001, "sodium_mg":0.1, "fibre_g":0.001}


def validate_nutrition_policy(policy: NutritionPolicy) -> None:
    if policy not in (STRICT_POLICY, WITHIN_GRADE_POLICY, LEGACY_MEANINGFUL_POLICY, DEFAULT_POLICY):
        raise ValueError("Unknown nutrition benefit policy")


def assess_nutrition_benefit(before: dict, after: dict, metric: str,
                             before_score: dict, after_score: dict, *,
                             policy: NutritionPolicy = DEFAULT_POLICY) -> dict:
    """Evaluate numbers only; callers must verify evidence, restrictions and edits."""
    validate_nutrition_policy(policy)
    within_grade = policy != STRICT_POLICY
    prototype = policy == DEFAULT_POLICY
    result = {"benefit_policy":policy, "status":"needs_evidence", "reasons":[],
        "nutrient_guard_policy":"rounding_only_no_regression_v1" if within_grade else "conservative_no_regression_v1",
        "nutrient_deltas_per_serving":{}, "target_points_saved":None}
    if prototype:
        result.update(nutrient_guard_policy='bounded_five_percent_tradeoffs_v1',
            maximum_other_nutrient_regression_relative=MAX_OTHER_NUTRIENT_REGRESSION, accepted_tradeoffs=[])
    reasons = result["reasons"]
    if metric not in SCORE_KEYS:
        result.update(status="blocked", reasons=["unsupported_production_nutrition_objective"])
        return result
    metrics = SCORE_METRICS if prototype or not within_grade else tuple(ROUNDING_TOLERANCE)
    if prototype:
        result['non_guard_nutrients'] = ['carbs_g', 'fat_g']
    def number(value):
        return not isinstance(value,bool) and isinstance(value,(float,int)) and math.isfinite(value) and value>=0
    if any(not number(side.get(m)) for side in (before,after) for m in metrics):
        reasons.append("complete_comparable_guard_nutrients_required")
        return result
    result['nutrient_deltas_per_serving'] = {m: after[m]-before[m] for m in ROUNDING_TOLERANCE
        if number(before.get(m)) and number(after.get(m))}
    grades = {"Nutriscore_"+letter:rank for rank,letter in enumerate("ABCDE")}
    left, right = before_score.get("nutri_score"), after_score.get("nutri_score")
    if left not in grades or right not in grades:
        result.update(status="blocked", reasons=["invalid_score_grade"])
        return result
    for m in metrics:
        change = after[m]-before[m]
        result["nutrient_deltas_per_serving"][m] = change
        worsened = change<0 if m in {"fibre_g","protein_g"} else change>0
        rounding = abs(change)<=ROUNDING_TOLERANCE[m]+1e-12 if within_grade else math.isclose(before[m],after[m],rel_tol=1e-9,abs_tol=1e-9)
        if prototype and m != metric and worsened and not rounding and abs(change) <= before[m]*MAX_OTHER_NUTRIENT_REGRESSION+1e-12:
            result['accepted_tradeoffs'].append({'metric':m, 'delta_per_serving':change,
                'relative_change':change/before[m]})
            continue
        if worsened and not rounding:
            reasons.append("nutrient_regression:"+m)
    key = SCORE_KEYS[metric]
    def points(score):
        return score.get("negative_points",{}).get("items",{}).get(key,{}).get("points")
    first,last = points(before_score),points(after_score)
    if number(first) and number(last):
        result["target_points_saved"] = first-last
    elif not within_grade:
        result["reasons"] = ["complete_score_points_required"]
        return result
    reduction = before[metric]-after[metric]
    if within_grade:
        if reduction<=0:reasons.append("no_target_nutrient_improvement")
        relative = reduction/before[metric] if before[metric]>0 else None
        result.update(minimum_reduction_per_serving=MIN_REDUCTION[metric],
            minimum_relative_reduction=MIN_RELATIVE_REDUCTION,
            rounding_tolerances_per_serving=dict(ROUNDING_TOLERANCE),
            target_benefit={"metric":metric,"before_per_serving":before[metric],
                "after_per_serving":after[metric],"reduction_per_serving":reduction,
                "relative_reduction":relative},
            validation="Thresholds are provisional engineering criteria, not clinical claims.")
        if reduction+1e-9<MIN_REDUCTION[metric]:reasons.append("target_reduction_below_absolute_minimum")
        if relative is None or relative+1e-12<MIN_RELATIVE_REDUCTION:reasons.append("target_reduction_below_relative_minimum")
        if grades[right]>grades[left]:reasons.append("nutrition_grade_regression")
    else:
        if first<MIN_TARGET_POINTS:reasons.append("target_below_production_threshold_on_comparable_inputs")
        if reduction<=0:reasons.append("no_target_nutrient_improvement")
        if first-last<=0:reasons.append("no_target_negative_point_improvement")
        if grades[right]>=grades[left]:reasons.append("no_strict_letter_grade_improvement")
    result["status"] = "rejected" if reasons else "approved_input_objective"
    return result
