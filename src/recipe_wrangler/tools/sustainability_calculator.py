# Purpose: Compute sustainability/carbon footprint totals via Chroma matches.

from typing import Dict, List, Optional
from textwrap import shorten

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.query_chromadb import query_sustainability_db

SOURCE_SUSTAINABILITY = "Sustainable FooDB"

@tool
def sustainability_tool_chroma(
    title: str,
    ingredient_names: List[str],
    weights: List[float],
    serving_size_g: Optional[float] = None,
    serves: Optional[float] = None,
    min_similarity: float = 0.5,
) -> Dict:
    """
    Compute recipe carbon footprint (kg CO2e). 
    Matches ingredients via Chroma.
    """
    details: List[Dict] = []
    total_sustainability = 0.0

    serves_value: Optional[float] = None
    if serves is not None:
        try:
            serves_value = float(serves)
        except (TypeError, ValueError) as exc:
            raise ValueError("sustainability_tool_chroma: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    for ing_name, weight_g in zip(ingredient_names, weights):
        # Defensive query: handle empty results
        matches = query_sustainability_db(ing_name) or []
        match = matches[0] if matches else None

        if not match:
            details.append({
                "ingredient": ing_name,
                "matched_sustainability_ingredient": None,
                "weight_g": float(weight_g),
                "cf_val": None,
                "distance": None,
                "contribution": 0.0,
                "source_sustainability": SOURCE_SUSTAINABILITY,
            })
            continue

        meta = (match.get("metadata") or {})
        cf_val = meta.get("cf_val")                 # kg CO2e per kg ingredient
        distance = meta.get("distance")             # lower is closer if cosine distance
        matched_name = meta.get("ingredient")

        # contribution in kg CO2e (weight_g -> kg)
        contribution = 0.0
        if cf_val is not None:
            contribution = (float(weight_g) / 1000.0) * float(cf_val)
            total_sustainability += contribution

        details.append({
            "ingredient": ing_name,
            "matched_sustainability_ingredient": matched_name,
            "weight_g": float(weight_g),
            "cf_val": None if cf_val is None else float(cf_val),
            "distance": None if distance is None else float(distance),
            "contribution": float(contribution),
            "source_sustainability": SOURCE_SUSTAINABILITY,
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
        "source_sustainability": SOURCE_SUSTAINABILITY,
    }


def Sustainability_Node(state: RecipeState) -> RecipeState:
    """
    Node to compute carbon footprint via Chroma, scale by ingredient weight/serves, 
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

    # Ensure equal lengths (tool zips the two lists)
    n = min(len(ingredient_names), len(weights_g))
    ingredient_names = ingredient_names[:n]
    weights_g = weights_g[:n]

    res = sustainability_tool_chroma.invoke({
        "title": state.title,
        "ingredient_names": ingredient_names,
        "weights": weights_g,                       # ✅ correct param name
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.5,
        "serving_size_g": state.serving_size_g,
        "serves": state.serves,
    })

    state.total_sustainability = res["total_sustainability"]                # kg CO2e
    state.total_sustainability_per_serving = res["total_sustainability_per_serving"]
    state.sustainability_per_kg = res["sustainability_per_kg"]              # kg CO2e/kg
    state.sustainability_details = res["details"]
    state.sustainability_serves = res.get("serves")

    if debug:
        print(f"\n[Sustainability_Node] Computed (ChromaDB) for recipe '{state.title}'.")
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
