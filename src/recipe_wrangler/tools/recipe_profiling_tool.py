# recipe_profiling.py

from typing import Any, Dict

from recipe_wrangler.tools.nutritional_calculator import nutritional_tool_chroma
from recipe_wrangler.tools.sustainability_calculator import (
    sustainability_tool_chroma,
)

def Recipe_Profiling_Tool(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run nutritional and sustainability profiling on a recipe
    and merge their results into a single dictionary.

    Args:
        payload (dict): The input dictionary with keys like
            - title (str)
            - ingredient_names (list[str])
            - measurements (list[str])
            - weights (list[float])
            - serving_size_g (float)
            - serves (int)
            - min_similarity (float)

    Returns:
        dict: Combined recipe profiling with nutritional + sustainability
              information merged per ingredient and recipe totals.
    """

    nutrition_payload = dict(payload)
    sustainability_payload = dict(payload)
    sustainability_payload.pop("source", None)

    nutrition_result = nutritional_tool_chroma.invoke(nutrition_payload)
    nutrition_source_key = nutrition_result.get("source_key", "unknown")
    sustainability_result = sustainability_tool_chroma.invoke(sustainability_payload)

    merged: Dict[str, Any] = {
        "title": payload.get("title", ""),
        "ingredients": [],
        "totals": {},
        "sustainability_per_kg": sustainability_result.get("sustainability_per_kg"),
        "sustainability_serves": sustainability_result.get("serves"),
    }
    merged["nutrition_source_key"] = nutrition_source_key
    merged["nutrition_source"] = nutrition_result.get("source")

    nutrition_details = nutrition_result.get("details", [])
    sustainability_details = sustainability_result.get("details", [])

    for i, ingredient in enumerate(payload["ingredient_names"]):
        ingredient_entry = {"ingredient": ingredient}

        if i < len(nutrition_details):
            ingredient_entry.update(nutrition_details[i])

        if i < len(sustainability_details):
            for k, v in sustainability_details[i].items():
                if k == "ingredient":
                    continue
                if k not in ingredient_entry:
                    ingredient_entry[k] = v
                else:
                    ingredient_entry[f"sustainability_{k}"] = v

        merged["ingredients"].append(ingredient_entry)

    totals = {}
    for source, prefix in [
        (nutrition_result, "nutrition"),
        (sustainability_result, "sustainability"),
    ]:
        for k, v in source.items():
            if k.startswith("total_"):
                key = k if prefix in {"nutrition", "sustainability"} else f"{prefix}_{k}"
                totals[key] = v

    merged["totals"] = totals

    return merged


from typing import Any, Dict, List, cast

def Recipe_Profiling_Node(state: "State") -> "State":
    """
    ode that runs nutrition + sustainability profiling for the recipe and writes the merged ingredient details, 
    totals, and source info back into the flow state.
    """
    serves = state.get("serves", 1) or 1
    names: List[str] = state.get("ingredient_names", []) or []
    measurements: List[str] = state.get("measurements", []) or []
    weights: List[float] = state.get("weights", []) or []

    nutrition_source = (
        state.get("nutrition_source")
        or state.get("nutritional_source")
        or state.get("source")
        or "irish"
    )

    payload: Dict[str, Any] = {
        "title": state.get("title") or "Untitled Recipe",
        "ingredient_names": names,
        "measurements": measurements,
        "weights": weights,
        "serving_size_g": state.get("serving_size_g")
            or (sum(weights) / serves if weights else 0.0),
        "serves": serves,
        "min_similarity": state.get("min_similarity", 0.5),
        "source": nutrition_source,
    }

    profile: Dict[str, Any] = Recipe_Profiling_Tool(payload)
    totals: Dict[str, float] = cast(Dict[str, float], profile.get("totals", {}))
    prof_items: List[Dict[str, Any]] = cast(List[Dict[str, Any]], profile.get("ingredients", []))
    nutrition_source_key = cast(str, profile.get("nutrition_source_key") or "unknown")
    suffix = f"_{nutrition_source_key}"

    merged: List[Dict[str, Any]] = []
    n = min(len(names), len(measurements), len(weights), len(prof_items))
    for i in range(n):
        p = dict(prof_items[i])  # copy
        # unify field names: set canonical surface name + parser fields
        p["name"] = names[i]
        p["measurement"] = measurements[i]
        p["weight_g"] = float(weights[i])  # ensure numeric
        merged.append(p)

    per_serving_suffix = f"_per_serving{suffix}"

    total_sustainability = totals.get("total_sustainability")
    total_sustainability_per_serving = totals.get("total_sustainability_per_serving")

    out: "State" = {
        "ingredients": merged,

        # keep convenient totals (flattened)
        "profiling_totals": totals,
        "serves": serves,
        "total_sustainability": total_sustainability,
        "total_sustainability_per_serving": total_sustainability_per_serving,
        "sustainability_per_kg": profile.get("sustainability_per_kg"),
        f"total_carbohydrate_g{per_serving_suffix}": totals.get(f"total_carbohydrate_g{per_serving_suffix}"),
        f"total_fat_g{per_serving_suffix}": totals.get(f"total_fat_g{per_serving_suffix}"),
        f"total_protein_g{per_serving_suffix}": totals.get(f"total_protein_g{per_serving_suffix}"),
        f"total_energy_kcal{per_serving_suffix}": totals.get(f"total_energy_kcal{per_serving_suffix}"),
        "nutrition_source": profile.get("nutrition_source") or nutrition_source,
        "nutrition_source_key": nutrition_source_key,

        # keep entire tool output (optional, handy for debugging)
        "full_profile": profile,
    }
    return out
 
