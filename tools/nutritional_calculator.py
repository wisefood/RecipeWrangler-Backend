from typing import List, Dict, Optional
from langchain.tools import tool
from utils.query_chromadb import query_nutritional_db_irish

# Canonical keys expected in Chroma metadata (per 100g)
PROTEIN_KEY = "Protein (g)"
CARB_KEY    = "Carbohydrate (g)"
FAT_KEY     = "Fat (g)"

# Additional macro/micronutrient keys (per 100g)
SUGAR_KEYS = [
    "Sugar (g)",
    "Sugars (g)",
    "Total Sugars (g)",
    "Sugars",
]
SATURATED_FAT_KEYS = [
    "Saturated Fat (g)",
    "SaturatedFat (g)",
    "Saturated Fat",
]
SODIUM_KEYS = [
    "Sodium (mg)",
    "Sodium_mg",
    "Sodium (milligrams)",
    "Sodium",
]
# Common metadata keys for kcal/100g, if present in the Chroma collection
ENERGY_KCAL_KEYS = [
    "Energy (kcal)",
    "Energy_kcal",
    "kcal",
    "Calories (kcal)",
    "Calories",
]


def _normalize_source_key(source: Optional[str]) -> str:
    text = (source or "").strip()
    if not text:
        return "unknown"
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in text)
    normalized = normalized.strip("_")
    return normalized or "unknown"


def _first_present(d: Dict, keys: List[str]) -> Optional[float]:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


def _make_markdown_table(rows: List[Dict]) -> str:
    header = (
        "| Ingredient | Match | Weight (g) | Prot/100g | Carb/100g | Fat/100g | Sugar/100g | SatFat/100g | Sodium/100g (mg) | "
        "Prot (g) | Carb (g) | Fat (g) | Sugar (g) | SatFat (g) | Sodium (mg) |\n"
    )
    sep    = (
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = []
    for r in rows:
        lines.append(
            f"| {r['ingredient']} | {r.get('matched_nutritional_ingredient') or '—'} | {r['weight_g']:.1f} | "
            f"{r['protein_per_100g']:.1f} | {r['carbs_per_100g']:.1f} | {r['fat_per_100g']:.1f} | "
            f"{r['sugars_per_100g']:.1f} | {r['saturated_fat_per_100g']:.1f} | {r['sodium_per_100g_mg']:.1f} | "
            f"{r['protein_g']:.1f} | {r['carbs_g']:.1f} | {r['fat_g']:.1f} | "
            f"{r['sugar_g']:.1f} | {r['saturated_fat_g']:.1f} | {r['sodium_mg']:.1f} |"
        )
    return header + sep + "\n".join(lines)

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
    include_markdown_table: bool = False,
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

    source_key = _normalize_source_key(source)
    total_suffix = f"_{source_key}"
    serves_value: Optional[float] = None
    if serves is not None:
        try:
            serves_value = float(serves)
        except (TypeError, ValueError) as exc:
            raise ValueError("nutritional_tool_chroma: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    # Clamp similarity into [0, 1] just in case
    min_similarity = max(0.0, min(1.0, float(min_similarity)))
    # Cosine distance = 1 - similarity
    max_distance = 1.0 - min_similarity

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
                "source": "Irish Composition Table",
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

        if distance is not None and float(distance) > max_distance:
            details.append({
                "ingredient": ing_name,
                "source": "Irish Composition Table",
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
                "distance": float(distance),
            })
            continue

        matched_name = meta.get("food_name") or match.get("document") or "—"

        # Pull macro values per 100g with safe fallbacks
        protein_per_100g = float(meta.get(PROTEIN_KEY, 0.0))
        carbs_per_100g   = float(meta.get(CARB_KEY, 0.0))
        fat_per_100g     = float(meta.get(FAT_KEY, 0.0))
        sugars_per_100g = _first_present(meta, SUGAR_KEYS) or 0.0
        saturated_fat_per_100g = _first_present(meta, SATURATED_FAT_KEYS) or 0.0
        sodium_per_100g_mg = _first_present(meta, SODIUM_KEYS) or 0.0

        # Try to read kcal/100g from metadata; if missing, approximate via 4/4/9
        energy_kcal_per_100g = _first_present(meta, ENERGY_KCAL_KEYS)
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
            "source": "Irish Composition Table",
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

    if include_markdown_table:
        result["table_markdown"] = _make_markdown_table(details)

    return result


def Nutrition_Node(state: dict) -> dict:
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
        "include_markdown_table": bool(state.get("include_markdown_table", False)),
        "source": source,
        "serves": state.get("serves"),
    })

    source_key = res.get("source_key") or _normalize_source_key(source)
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
        "nutritional_table_markdown": res.get("table_markdown"),
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
