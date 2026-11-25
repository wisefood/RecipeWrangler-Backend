# Purpose: Compute recipe carbon footprint totals from the reviewed AGRIBALYSE 3.2 ingredient mapping (see sustainability_factors).

import math
from typing import Dict, List, Optional, Tuple

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.tools.sustainability_factors import factor_for

SOURCE_LABEL = {"agribalyse": "AGRIBALYSE 3.2", "concito": "CONCITO v1.2 (fallback)", "estimate": "role average (estimate)"}
POLICY_VERSION = "agribalyse-3.2-reviewed-name-mapping-v1"


def best_sustainability_match(ing_name: str, weight_g: float = 0.0, region: Optional[str] = None) -> Tuple[Optional[float], Optional[str], str]:
    """(cf_val kg CO2e/kg, matched record, confidence). cf_val is None when no rule covers the ingredient (it then contributes 0 CO2e).
    Confidence: source_reviewed (AGRIBALYSE rule), fallback (CONCITO), estimate (role average), none."""
    cf, source, record = factor_for(ing_name, weight_g, region)
    if cf is None:
        return None, None, "none"
    return float(cf), record, {"agribalyse": "source_reviewed", "concito": "fallback", "estimate": "estimate"}[source]


def _to_float_or_none(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

@tool
def sustainability_tool_vector(
    title: str,
    ingredient_names: List[str],
    weights: List[float],
    serving_size_g: Optional[float] = None,
    serves: Optional[float] = None,
    min_similarity: float = 0.5,
    region: Optional[str] = None,
) -> Dict:
    """
    Compute recipe carbon footprint (kg CO2e). 
    Ingredient names are mapped to AGRIBALYSE 3.2 records by reviewed name rules (CONCITO and role averages only complete the total).
    """
    details: List[Dict] = []
    total_sustainability = 0.0
    if len(ingredient_names) != len(weights):
        raise ValueError("Sustainability ingredients and gram weights must have equal lengths.")
    weights = [float(weight) for weight in weights]
    if any(not math.isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("Sustainability weights must be finite, nonnegative grams.")
    serves_value: Optional[float] = None
    if serves is not None:
        try:
            serves_value = float(serves)
        except (TypeError, ValueError) as exc:
            raise ValueError("sustainability_tool_vector: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    sources = set()
    for ing_name, weight_g in zip(ingredient_names, weights):
        cf_val, matched_name, conf = best_sustainability_match(ing_name, weight_g, region)
        source = {"source_reviewed": "agribalyse", "fallback": "concito", "estimate": "estimate"}.get(conf)
        sources.add(source)
        provenance = {
            "sustainability_policy": POLICY_VERSION,
            "sustainability_provenance_status": "source_verified" if source == "agribalyse" else "fallback_or_estimate" if source else "no_factor",
            "sustainability_calculation_status": "matched" if cf_val is not None else "missing_factor_partial_total",
            "sustainability_scope": "FR consumer-product estimate (AGRIBALYSE no-preparation scenario); recipe cooking excluded",
        }
        contribution = 0.0 if cf_val is None else (float(weight_g) / 1000.0) * float(cf_val)
        total_sustainability += contribution
        details.append({
            "ingredient": ing_name,
            "matched_sustainability_ingredient": matched_name,
            "weight_g": float(weight_g),
            "cf_val": None if cf_val is None else float(cf_val),
            "distance": None,
            "contribution": float(contribution),
            "sustainability_match_confidence": conf,
            "source_sustainability": SOURCE_LABEL.get(source),
            **provenance,
        })

    # Normalize to kg CO2e per kg of prepared recipe (optional)
    sustainability_per_kg: Optional[float] = None
    if serving_size_g is not None and serves_value is not None:
        try:
            total_weight_g = float(serving_size_g) * serves_value
            if total_weight_g > 0:
                sustainability_per_kg = (total_sustainability * 1000.0) / total_weight_g
        except (TypeError, ValueError):
            sustainability_per_kg = None

    total_sustainability_per_serving: Optional[float] = None
    if serves_value:
        total_sustainability_per_serving = total_sustainability / serves_value

    recipe_status = "partial" if any(d["cf_val"] is None and d["weight_g"] > 0 for d in details) else "complete_estimate"
    for detail in details:
        detail["recipe_sustainability_status"] = recipe_status

    return {
        "title": title,
        "details": details,
        "total_sustainability": float(total_sustainability),
        "total_sustainability_per_serving": None
        if total_sustainability_per_serving is None
        else float(total_sustainability_per_serving),
        "sustainability_per_kg": None
        if sustainability_per_kg is None
        else float(sustainability_per_kg),
        "serves": serves_value,
        "source_sustainability": SOURCE_LABEL["agribalyse"] + (" + fallback" if sources & {"concito", "estimate"} else ""),
        "sustainability_policy": POLICY_VERSION,
        "sustainability_status": recipe_status,
    }


def Sustainability_Node(state: RecipeState) -> RecipeState:
    """
    Node to compute carbon footprint from the AGRIBALYSE name mapping, scale by ingredient weight/serves,
    and store per-ingredient details plus per-serving/total CO2e in state.
    """
    debug = bool(state.debug)

    ingredient_names = state.ingredient_names or []
    if not isinstance(ingredient_names, list):
        raise ValueError("Sustainability_Node: 'ingredient_names' must be a list of strings.")

    # Pull gram weights from Weight_Calculator output
    weights_g = None
    if isinstance(state.weights, dict):
        weights_g = state.weights.get("weights")
    elif isinstance(state.weights, list):
        weights_g = state.weights

    if weights_g is None:
        raise ValueError("Sustainability_Node: missing 'weights' (grams) from Weight_Calculator.")

    try:
        weights_g = [float(x) for x in weights_g]
    except (TypeError, ValueError) as e:
        raise ValueError("Sustainability_Node: all weights must be numeric (grams).") from e

    if len(ingredient_names) != len(weights_g):
        raise ValueError("Sustainability_Node: ingredient and weight counts differ.")

    res = sustainability_tool_vector.invoke({
        "title": state.title,
        "ingredient_names": ingredient_names,
        "weights": weights_g,                       # ✅ correct param name
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.5,
        "serving_size_g": state.serving_size_g,
        "serves": state.serves,
        "region": state.region,
    })

    state.total_sustainability = res["total_sustainability"]                # kg CO2e
    state.total_sustainability_per_serving = res["total_sustainability_per_serving"]
    state.sustainability_per_kg = res["sustainability_per_kg"]              # kg CO2e/kg
    state.sustainability_details = res["details"]
    state.sustainability_serves = res.get("serves")

    if debug:
        print(f"\n[Sustainability_Node] Computed (AGRIBALYSE mapping) for recipe '{state.title}'.")
        print(f"   total_sustainability = {res['total_sustainability']:.4f} kg CO2e")
        per_serving = res.get("total_sustainability_per_serving")
        if per_serving is not None:
            print(f"   sustainability/serving = {per_serving:.4f} kg CO2e")
        else:
            print("   sustainability/serving = None (serves missing)")
        if res["sustainability_per_kg"] is not None:
            print(f"   sustainability_per_kg = {res['sustainability_per_kg']:.4f} kg CO2e/kg")
        else:
            print("   sustainability_per_kg = None (serving info missing)")
        print(f"\n[Sustainability_Node] Updated State Keys: {list(state.model_dump().keys())}")

    return state
