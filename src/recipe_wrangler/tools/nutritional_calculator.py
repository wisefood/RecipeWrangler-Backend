from typing import Dict, List, Optional
from langchain.tools import tool
from recipe_wrangler.utils.query_chromadb import query_nutritional_db_irish

SOURCE_NUTRITION = "Irish Composition Table"

PROTEIN_KEY = "Protein (g)"
CARB_KEY    = "Carbohydrate (g)"
FAT_KEY     = "Fat (g)"


@tool(
    "nutritional_tool_chroma",
    description=(
        "Compute a recipe's nutritional profile (protein, carbs, fat, sugar, saturated fat, sodium, kcal) using ChromaDB matches. "
        "Assumes cosine distance (lower is better) and enforces a minimum cosine similarity threshold. "
        "Parameter 'source' selects the composition table (default: 'irish')."
    ),
)
def nutritional_tool_chroma(
    title: str,
    ingredient_names: List[str],
    weights: List[float],
    min_similarity: float = 0.5,
    source: str = "irish",
    serves: Optional[float] = None,
) -> Dict:
    details: List[Dict] = []
    total_protein_g = 0.0
    total_carbs_g   = 0.0
    total_fat_g     = 0.0
    total_energy_kcal = 0.0
    total_sugar_g = 0.0
    total_saturated_fat_g = 0.0
    total_sodium_mg = 0.0

    source_key = source or "unknown"
    total_suffix = f"_{source_key}"
    serves_value: Optional[float] = None
    if serves is not None:
        try:
            serves_value = float(serves)
        except (TypeError, ValueError) as exc:
            raise ValueError("nutritional_tool_chroma: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    # Select query function based on source; currently only 'irish' supported
    def _query_nutrition(name: str):
        if (source or "").lower() == "irish":
            return query_nutritional_db_irish(name)
        # Fallback to Irish until other sources are implemented
        return query_nutritional_db_irish(name)

    for ing_name, weight_g in zip(ingredient_names, weights):
        matches = _query_nutrition(ing_name) or []
        match = matches[0] if matches else None

        if not match:
            details.append({
                "ingredient": ing_name,
                "source": SOURCE_NUTRITION,
                "source_nutrition": SOURCE_NUTRITION,
                "matched_nutritional_ingredient": None,
                "weight_g": float(weight_g),
                "protein_per_100g": 0.0,
                "carbs_per_100g": 0.0,
                "fat_per_100g": 0.0,
                "sugars_per_100g": 0.0,
                "saturated_fat_per_100g": 0.0,
                "sodium_per_100g_mg": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
                "sugar_g": 0.0,
                "saturated_fat_g": 0.0,
                "sodium_mg": 0.0,
                "distance": None,
            })
            continue

        meta = match.get("metadata") or {}
        # In your dataset, distance is top-level
        distance = match.get("distance", None)

        matched_name = meta.get("food_name") or match.get("document") or "—"

        # Pull macro values per 100g with safe fallbacks
        protein_per_100g = float(meta.get(PROTEIN_KEY, 0.0))
        carbs_per_100g   = float(meta.get(CARB_KEY, 0.0))
        fat_per_100g     = float(meta.get(FAT_KEY, 0.0))
        try:
            sugars_per_100g = float(meta.get("Sugar (g)", 0.0))
        except (TypeError, ValueError):
            sugars_per_100g = 0.0
        try:
            saturated_fat_per_100g = float(meta.get("Saturated Fat (g)", 0.0))
        except (TypeError, ValueError):
            saturated_fat_per_100g = 0.0
        try:
            sodium_per_100g_mg = float(meta.get("Sodium (mg)", 0.0))
        except (TypeError, ValueError):
            sodium_per_100g_mg = 0.0

        # Try to read kcal/100g from metadata; if missing, approximate via 4/4/9
        try:
            energy_kcal_per_100g = float(meta.get("Energy (kcal)", 0.0))
        except (TypeError, ValueError):
            energy_kcal_per_100g = 0.0
        if energy_kcal_per_100g is None:
            # Atwater factors (approximate): 4 kcal/g protein, 4 kcal/g carbs, 9 kcal/g fat
            energy_kcal_per_100g = 4.0 * protein_per_100g + 4.0 * carbs_per_100g + 9.0 * fat_per_100g

        scale = float(weight_g) / 100.0
        protein_g = scale * protein_per_100g
        carbs_g   = scale * carbs_per_100g
        fat_g     = scale * fat_per_100g
        sugar_g = scale * float(sugars_per_100g)
        saturated_fat_g = scale * float(saturated_fat_per_100g)
        sodium_mg = scale * float(sodium_per_100g_mg)
        energy_kcal = scale * float(energy_kcal_per_100g)

        total_protein_g += protein_g
        total_carbs_g   += carbs_g
        total_fat_g     += fat_g
        total_sugar_g   += sugar_g
        total_saturated_fat_g += saturated_fat_g
        total_sodium_mg += sodium_mg

        details.append({
            "ingredient": ing_name,
            "source": SOURCE_NUTRITION,
            "source_nutrition": SOURCE_NUTRITION,
            "matched_nutritional_ingredient": matched_name,
            "weight_g": float(weight_g),
            "protein_per_100g": protein_per_100g,
            "carbs_per_100g": carbs_per_100g,
            "fat_per_100g": fat_per_100g,
            "sugars_per_100g": float(sugars_per_100g),
            "saturated_fat_per_100g": float(saturated_fat_per_100g),
            "sodium_per_100g_mg": float(sodium_per_100g_mg),
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "sugar_g": sugar_g,
            "saturated_fat_g": saturated_fat_g,
            "sodium_mg": sodium_mg,
            "energy_kcal_per_100g": float(energy_kcal_per_100g),
            "energy_kcal": float(energy_kcal),
            "distance": None if distance is None else float(distance),
        })

        total_energy_kcal += energy_kcal

    result: Dict = {
        "title": title,
        "details": details,
        "source": source,
        "source_nutrition": SOURCE_NUTRITION,
        "source_key": source_key,
        "serves": serves_value,
    }

    totals_map = {
        "protein_g": total_protein_g,
        "carbohydrate_g": total_carbs_g,
        "fat_g": total_fat_g,
        "energy_kcal": total_energy_kcal,
        "sugar_g": total_sugar_g,
        "saturated_fat_g": total_saturated_fat_g,
        "sodium_mg": total_sodium_mg,
    }

    for metric, value in totals_map.items():
        per_serving_key = f"total_{metric}_per_serving{total_suffix}"

        if serves_value:
            result[per_serving_key] = float(value / serves_value)
        else:
            result[per_serving_key] = None

    return result


def Nutrition_Node(state: dict) -> dict:
    """
    Node to compute nutrition via Chroma, scale by weight/serves, store totals and details in state.
    """
    
    debug = bool(state.get("debug", False))

    ingredient_names = state.get("ingredient_names") or []
    if not isinstance(ingredient_names, list):
        raise ValueError("Nutrition_Node: 'ingredient_names' must be a list of strings.")

    weights = None
    if isinstance(state.get("weights"), dict):
        weights = state["weights"].get("weights")
    elif isinstance(state.get("weights"), list):
        weights = state["weights"]

    if weights is None:
        raise ValueError("Nutrition_Node: missing 'weights' (grams) next to 'ingredient_names'.")

    try:
        weights = [float(x) for x in weights]
    except (TypeError, ValueError) as e:
        raise ValueError("Nutrition_Node: all weights must be numeric (grams).") from e

    n = min(len(ingredient_names), len(weights))
    ingredient_names = ingredient_names[:n]
    weights = weights[:n]

    source = (
        state.get("nutrition_source")
        or state.get("nutritional_source")
        or state.get("source")
        or "irish"
    )

    res = nutritional_tool_chroma.invoke({
        "title": state.get("title", "Untitled Recipe"),
        "ingredient_names": ingredient_names,
        "weights": weights,
        "min_similarity": state.get("min_similarity", 0.5),
        "source": source,
        "serves": state.get("serves"),
    })

    source_key = res.get("source_key") or (source or "unknown")
    suffix = f"_{source_key}"
    per_serving_suffix = f"_per_serving{suffix}"

    totals_per_serving = {
        f"protein_g{per_serving_suffix}": res.get(f"total_protein_g{per_serving_suffix}"),
        f"carbohydrate_g{per_serving_suffix}": res.get(f"total_carbohydrate_g{per_serving_suffix}"),
        f"fat_g{per_serving_suffix}": res.get(f"total_fat_g{per_serving_suffix}"),
        f"energy_kcal{per_serving_suffix}": res.get(f"total_energy_kcal{per_serving_suffix}"),
        f"sugar_g{per_serving_suffix}": res.get(f"total_sugar_g{per_serving_suffix}"),
        f"saturated_fat_g{per_serving_suffix}": res.get(f"total_saturated_fat_g{per_serving_suffix}"),
        f"sodium_mg{per_serving_suffix}": res.get(f"total_sodium_mg{per_serving_suffix}"),
    }

    state.update({
        "nutritional_totals": totals_per_serving,
        "nutritional_details": res["details"],
        "nutritional_source": source,
        "nutrition_serves": res.get("serves"),
    })

    if debug:
        print(f"\n[Nutrition_Node] Computed (ChromaDB) for recipe '{state.get('title', 'Untitled Recipe')}'.")
        serves = res.get("serves")
        if serves:
            print(f"   Serves:             {serves:.2f}")
        metrics = [
            ("Protein", "protein_g", "g"),
            ("Carbohydrate", "carbohydrate_g", "g"),
            ("Fat", "fat_g", "g"),
            ("Energy", "energy_kcal", "kcal"),
            ("Sugar", "sugar_g", "g"),
            ("Saturated fat", "saturated_fat_g", "g"),
            ("Sodium", "sodium_mg", "mg"),
        ]
        for label, metric, unit in metrics:
            key = f"total_{metric}{per_serving_suffix}"
            value = res.get(key)
            if value is not None:
                print(f"   {label} / serving:  {value:.2f} {unit}")
            else:
                print(f"   {label} / serving:  N/A")
        print(f"\n[Nutrition_Node] Updated State Keys: {list(state.keys())}")

    return state
