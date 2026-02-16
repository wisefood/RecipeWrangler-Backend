# Purpose: Compute nutrition totals from ingredient weights via Chroma matches.

from typing import Dict, List, Optional

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.nutrition_postgres_v2 import (
    fetch_ingredient_nutrition_by_usda_id,
    fetch_ingredient_nutrition_by_canonical_id_irish,
)
from recipe_wrangler.utils.query_chromadb import (
    query_nutritional_db_irish,
    query_nutritional_db_usda,
)

SOURCE_NUTRITION = "Irish Composition Table"

PROTEIN_KEY = "Protein (g)"
CARB_KEY    = "Carbohydrate (g)"
FAT_KEY     = "Fat (g)"
SUGARS_KEY = "Total sugars (g)"
SATURATED_FAT_KEY = "Satd FA /100g fd (g)"
SODIUM_KEY = "Sodium (mg)"
ENERGY_KCAL_KEY = "Energy (kcal) (kcal)"
ENERGY_KJ_KEY = "Energy (kJ) (kJ)"
SOURCE_NUTRITION_USDA = "USDA Nutrients"

def _to_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _nutrient_value(raw: object, default: float = 0.0) -> float:
    """
    USDA nutrients are stored as nested objects like {"value": 12.3, "unit": "g"}.
    Irish values are plain numeric-like strings.
    """
    if isinstance(raw, dict):
        return _to_float(raw.get("value"), default=default)
    return _to_float(raw, default=default)


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
    total_fibre_g = 0.0

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

    source_normalized = (source or "irish").strip().lower()

    # Select query function based on source
    def _query_nutrition(name: str):
        if source_normalized == "irish":
            return query_nutritional_db_irish(name)
        if source_normalized == "usda":
            return query_nutritional_db_usda(name)
        return query_nutritional_db_irish(name)

    for ing_name, weight_g in zip(ingredient_names, weights):
        matches = _query_nutrition(ing_name) or []
        match = matches[0] if matches else None

        if not match:
            details.append({
                "ingredient": ing_name,
                "source": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "source_nutrition": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "matched_nutritional_ingredient": None,
                "canonical_food_id": None,
                "weight_g": float(weight_g),
                "protein_per_100g": 0.0,
                "carbs_per_100g": 0.0,
                "fat_per_100g": 0.0,
                "sugars_per_100g": 0.0,
                "saturated_fat_per_100g": 0.0,
                "sodium_per_100g_mg": 0.0,
                "fibre_per_100g": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
                "sugar_g": 0.0,
                "saturated_fat_g": 0.0,
                "sodium_mg": 0.0,
                "fibre_g": 0.0,
                "distance": None,
            })
            continue

        chroma_meta = match.get("metadata") or {}
        # In your dataset, distance is top-level
        distance = match.get("distance", None)
        similarity = None if distance is None else (1.0 - float(distance))
        if similarity is not None and similarity < float(min_similarity):
            details.append({
                "ingredient": ing_name,
                "source": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "source_nutrition": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "matched_nutritional_ingredient": None,
                "canonical_food_id": None,
                "weight_g": float(weight_g),
                "protein_per_100g": 0.0,
                "carbs_per_100g": 0.0,
                "fat_per_100g": 0.0,
                "sugars_per_100g": 0.0,
                "saturated_fat_per_100g": 0.0,
                "sodium_per_100g_mg": 0.0,
                "fibre_per_100g": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
                "sugar_g": 0.0,
                "saturated_fat_g": 0.0,
                "sodium_mg": 0.0,
                "fibre_g": 0.0,
                "distance": None if distance is None else float(distance),
                "similarity": similarity,
            })
            continue

        canonical_food_id = chroma_meta.get("canonical_food_id")
        usda_id = chroma_meta.get("usda_id")
        nutrient_row = None
        if source_normalized == "irish":
            if canonical_food_id:
                nutrient_row = fetch_ingredient_nutrition_by_canonical_id_irish(
                    str(canonical_food_id)
                )
        else:
            if usda_id:
                nutrient_row = fetch_ingredient_nutrition_by_usda_id(str(usda_id))

        if not nutrient_row:
            details.append({
                "ingredient": ing_name,
                "source": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "source_nutrition": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
                "matched_nutritional_ingredient": None,
                "canonical_food_id": canonical_food_id if source_normalized == "irish" else usda_id,
                "weight_g": float(weight_g),
                "protein_per_100g": 0.0,
                "carbs_per_100g": 0.0,
                "fat_per_100g": 0.0,
                "sugars_per_100g": 0.0,
                "saturated_fat_per_100g": 0.0,
                "sodium_per_100g_mg": 0.0,
                "fibre_per_100g": 0.0,
                "protein_g": 0.0,
                "carbs_g": 0.0,
                "fat_g": 0.0,
                "sugar_g": 0.0,
                "saturated_fat_g": 0.0,
                "sodium_mg": 0.0,
                "fibre_g": 0.0,
                "distance": None if distance is None else float(distance),
                "similarity": similarity,
            })
            continue

        meta = nutrient_row

        matched_name = (
            meta.get("Food Name")
            or meta.get("food_name")
            or chroma_meta.get("title")
            or match.get("document")
            or "—"
        )

        if source_normalized == "irish":
            # Pull macro values per 100g with safe fallbacks
            protein_per_100g = _to_float(meta.get(PROTEIN_KEY, 0.0))
            carbs_per_100g = _to_float(meta.get(CARB_KEY, 0.0))
            fat_per_100g = _to_float(meta.get(FAT_KEY, 0.0))
            sugars_per_100g = _to_float(meta.get(SUGARS_KEY, 0.0))
            saturated_fat_per_100g = _to_float(meta.get(SATURATED_FAT_KEY, 0.0))
            sodium_per_100g_mg = _to_float(meta.get(SODIUM_KEY, 0.0))
            fibre_per_100g = _to_float(meta.get("Fibre (g)", meta.get("Fiber (g)", 0.0)))

            # Try to read kcal/100g from metadata; if missing, approximate via 4/4/9
            energy_kcal_per_100g = _to_float(meta.get(ENERGY_KCAL_KEY), default=0.0)
            if energy_kcal_per_100g <= 0:
                energy_kcal_per_100g = None

            if not energy_kcal_per_100g:
                energy_kj_per_100g = _to_float(meta.get(ENERGY_KJ_KEY), default=0.0)
                if energy_kj_per_100g <= 0:
                    energy_kj_per_100g = None
                if energy_kj_per_100g:
                    energy_kcal_per_100g = energy_kj_per_100g / 4.184
                else:
                    # Atwater factors (approximate): 4 kcal/g protein, 4 kcal/g carbs, 9 kcal/g fat
                    energy_kcal_per_100g = (
                        4.0 * protein_per_100g + 4.0 * carbs_per_100g + 9.0 * fat_per_100g
                    )
        else:
            nutrients = meta.get("nutrients") or {}
            protein_per_100g = _nutrient_value(nutrients.get("Protein"), 0.0)
            carbs_per_100g = _nutrient_value(nutrients.get("Carbohydrate, by difference"), 0.0)
            fat_per_100g = _nutrient_value(nutrients.get("Total lipid (fat)"), 0.0)
            sugars_per_100g = _nutrient_value(
                nutrients.get("Sugars, total including NLEA", nutrients.get("Sugars, total")),
                0.0,
            )
            saturated_fat_per_100g = _nutrient_value(
                nutrients.get("Fatty acids, total saturated"), 0.0
            )
            sodium_per_100g_mg = _nutrient_value(nutrients.get("Sodium, Na"), 0.0)
            fibre_per_100g = _nutrient_value(nutrients.get("Fiber, total dietary"), 0.0)

            energy_kj_per_100g = _nutrient_value(nutrients.get("Energy"), 0.0)
            if energy_kj_per_100g > 0:
                energy_kcal_per_100g = energy_kj_per_100g / 4.184
            else:
                energy_kcal_per_100g = (
                    4.0 * protein_per_100g + 4.0 * carbs_per_100g + 9.0 * fat_per_100g
                )

        scale = float(weight_g) / 100.0
        protein_g = scale * protein_per_100g
        carbs_g   = scale * carbs_per_100g
        fat_g     = scale * fat_per_100g
        sugar_g = scale * float(sugars_per_100g)
        saturated_fat_g = scale * float(saturated_fat_per_100g)
        sodium_mg = scale * float(sodium_per_100g_mg)
        fibre_g = scale * float(fibre_per_100g)
        energy_kcal = scale * float(energy_kcal_per_100g)

        total_protein_g += protein_g
        total_carbs_g   += carbs_g
        total_fat_g     += fat_g
        total_sugar_g   += sugar_g
        total_saturated_fat_g += saturated_fat_g
        total_sodium_mg += sodium_mg
        total_fibre_g += fibre_g

        details.append({
            "ingredient": ing_name,
            "source": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
            "source_nutrition": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
            "matched_nutritional_ingredient": matched_name,
            "canonical_food_id": canonical_food_id if source_normalized == "irish" else usda_id,
            "weight_g": float(weight_g),
            "protein_per_100g": protein_per_100g,
            "carbs_per_100g": carbs_per_100g,
            "fat_per_100g": fat_per_100g,
            "sugars_per_100g": float(sugars_per_100g),
            "saturated_fat_per_100g": float(saturated_fat_per_100g),
            "sodium_per_100g_mg": float(sodium_per_100g_mg),
            "fibre_per_100g": float(fibre_per_100g),
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "sugar_g": sugar_g,
            "saturated_fat_g": saturated_fat_g,
            "sodium_mg": sodium_mg,
            "fibre_g": fibre_g,
            "energy_kcal_per_100g": float(energy_kcal_per_100g),
            "energy_kcal": float(energy_kcal),
            "distance": None if distance is None else float(distance),
            "similarity": similarity,
        })

        total_energy_kcal += energy_kcal

    result: Dict = {
        "title": title,
        "details": details,
        "source": source,
        "source_nutrition": SOURCE_NUTRITION if source_normalized == "irish" else SOURCE_NUTRITION_USDA,
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
        "fibre_g": total_fibre_g,
    }

    for metric, value in totals_map.items():
        total_key = f"total_{metric}{total_suffix}"
        per_serving_key = f"total_{metric}_per_serving{total_suffix}"
        result[total_key] = float(value)

        if serves_value:
            result[per_serving_key] = float(value / serves_value)
        else:
            result[per_serving_key] = None

    return result


def Nutrition_Node(state: RecipeState) -> RecipeState:
    """
    Node to compute nutrition via Chroma, scale by weight/serves, store totals and details in state.
    """
    
    debug = bool(state.debug)

    ingredient_names = state.ingredient_names or []
    if not isinstance(ingredient_names, list):
        raise ValueError("Nutrition_Node: 'ingredient_names' must be a list of strings.")

    weights = None
    if isinstance(state.weights, dict):
        weights = state.weights.get("weights")
    elif isinstance(state.weights, list):
        weights = state.weights

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
        getattr(state, "nutrition_source", None)
        or getattr(state, "nutritional_source", None)
        or getattr(state, "source", None)
        or "irish"
    )

    res = nutritional_tool_chroma.invoke({
        "title": state.title or "Untitled Recipe",
        "ingredient_names": ingredient_names,
        "weights": weights,
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.5,
        "source": source,
        "serves": state.serves,
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

    state.nutritional_totals = totals_per_serving
    state.nutritional_details = res["details"]
    state.nutritional_source = source
    state.nutrition_serves = res.get("serves")

    if debug:
        print(
            f"\n[Nutrition_Node] Computed (ChromaDB) for recipe "
            f"'{state.title or 'Untitled Recipe'}'."
        )
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
        print(f"\n[Nutrition_Node] Updated State Keys: {list(state.model_dump().keys())}")

    return state
