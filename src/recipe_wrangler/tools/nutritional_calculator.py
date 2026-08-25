# Purpose: Compute nutrition totals from ingredient weights via Elasticsearch matches.

import logging
import os
from typing import Dict, List, Optional

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.repositories.postgres_nutrition import (
    get_eu_ingredient_nutrition,
    get_hungarian_ingredient_nutrition,
    get_irish_ingredient_nutrition,
    get_slovenian_ingredient_nutrition,
)
from recipe_wrangler.tools.nutrition_match import best_nutrition_match

# Whether a `weak`-confidence ingredient match may contribute nutrients.
# Off means the old behaviour: any nearest neighbour is used, however
# implausible. Escape hatch only — leave it on.
REJECT_WEAK_NUTRITION_MATCHES = (
    os.getenv('NUTRITION_REJECT_WEAK_MATCHES', '1').strip().lower()
    not in {'0', 'false', 'no'}
)

logger = logging.getLogger(__name__)

SOURCE_NUTRITION = "Irish Composition Table"
SOURCE_NUTRITION_EU = "EU Composite (Ciqual+CoFID+NEVO)"

PROTEIN_KEY = "Protein (g)"
CARB_KEY    = "Carbohydrate (g)"
FAT_KEY     = "Fat (g)"
SUGARS_KEY = "Total sugars (g)"
SATURATED_FAT_KEY = "Satd FA /100g fd (g)"
SODIUM_KEY = "Sodium (mg)"
ENERGY_KCAL_KEY = "Energy (kcal) (kcal)"
ENERGY_KJ_KEY = "Energy (kJ) (kJ)"
SOURCE_NUTRITION_HUNGARIAN = "Hungarian Composition Table"
SOURCE_NUTRITION_SLOVENIAN = "Slovenian Composition Tables"
HUNGARIAN_PROTEIN_KEYS = ("Protein g", "Protein (g)")
HUNGARIAN_CARB_KEYS = ("Carbohydrat\nes g", "Carbohydrates g", "Carbohydrate (g)")
HUNGARIAN_FAT_KEYS = ("Fat g", "Fat (g)")
HUNGARIAN_SODIUM_KEYS = ("Sodium\nmg", "Sodium mg", "Sodium (mg)")
HUNGARIAN_ENERGY_KCAL_KEYS = ("Energy\nkcal", "Energy (kcal) (kcal)")
HUNGARIAN_ENERGY_KJ_KEYS = ("Energy\nkJ", "Energy (kJ) (kJ)")

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
    EU and Slovenian nutrients are stored as nested objects like
    {"value": 12.3, "unit": "g"}; Irish values are plain numeric-like strings.
    """
    if isinstance(raw, dict):
        return _to_float(raw.get("value"), default=default)
    return _to_float(raw, default=default)


def _first_present(meta: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in meta:
            return meta.get(key)
    return None


def _first_float(meta: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    return _to_float(_first_present(meta, keys), default=default)


def _source_label(source_key: str) -> str:
    if source_key == "hungarian":
        return SOURCE_NUTRITION_HUNGARIAN
    if source_key == "eu":
        return SOURCE_NUTRITION_EU
    if source_key == "slovenian":
        return SOURCE_NUTRITION_SLOVENIAN
    return SOURCE_NUTRITION


@tool(
    "nutritional_tool_vector",
    description=(
        "Compute a recipe's nutritional profile (protein, carbs, fat, sugar, saturated fat, sodium, kcal) using Elasticsearch matches. "
        "Assumes cosine distance (lower is better) and enforces a minimum cosine similarity threshold. "
        "Parameter 'source' selects the composition table (default: 'irish')."
    ),
)
def nutritional_tool_vector(
    title: str,
    ingredient_names: List[str],
    weights: List[float],
    min_similarity: float = 0.7,
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
            raise ValueError("nutritional_tool_vector: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    source_normalized = (source or "irish").strip().lower()
    supported_sources = {"irish", "hungarian", "eu", "slovenian"}
    if source_normalized not in supported_sources:
        raise ValueError(
            f"Unsupported nutrition source '{source_normalized}'. "
            "Supported sources: irish, hungarian, eu, slovenian"
        )

    for ing_name, weight_g in zip(ingredient_names, weights):
        m = best_nutrition_match(ing_name, source_normalized, float(min_similarity))
        match = m.get("match")
        active_source = m.get("source_key") or source_normalized
        match_confidence = m.get("confidence")
        match_reason = m.get("reason")
        distance = None if match is None else match.get("distance")
        similarity = m.get("similarity")

        # A low-confidence match contributes nothing.
        #
        # `best_nutrition_match` already grades every match
        # (curated / strong / weak / none) but nothing downstream ever read the
        # grade, so a weak match was consumed exactly like a curated one. That
        # is how "1 litre vegetable stock" became 1kg of
        # "Shortening, vegetable, household" — 1,018 g of fat, 254 g per
        # serving, and a Nutri-Score of D for a lentil soup.
        #
        # Zeroing the contribution is the honest outcome: it flows into
        # `nutrition_coverage` and raises `low_coverage`, so the caller learns
        # the profile is incomplete instead of receiving a confident, wrong
        # number. The near-match is still reported for review — the reason it
        # was rejected is more useful than pretending it did not happen.
        rejected_weak = (
            match is not None
            and match_confidence == "weak"
            and REJECT_WEAK_NUTRITION_MATCHES
        )
        if rejected_weak:
            logger.info(
                "nutrition: rejecting weak match %r -> %r (%s)",
                ing_name,
                m.get("matched_name"),
                match_reason,
            )

        if match is None or rejected_weak:
            details.append({
                "ingredient": ing_name,
                "source": _source_label(active_source),
                "source_nutrition": _source_label(active_source),
                "matched_nutritional_ingredient": (
                    m.get("matched_name") if rejected_weak else None
                ),
                "rejected_low_confidence": bool(rejected_weak),
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
                "similarity": similarity,
                "match_confidence": match_confidence,
                "match_reason": match_reason,
            })
            continue

        vector_metadata = match.get("metadata") or {}
        canonical_food_id = vector_metadata.get("canonical_food_id")
        eu_id = vector_metadata.get("eu_id")
        slovenian_id = vector_metadata.get("slovenian_id")
        nutrient_row = None
        if active_source == "irish":
            if canonical_food_id:
                nutrient_row = get_irish_ingredient_nutrition(
                    str(canonical_food_id)
                )
        elif active_source == "hungarian":
            if canonical_food_id:
                nutrient_row = get_hungarian_ingredient_nutrition(
                    str(canonical_food_id)
                )
        elif active_source == "eu":
            if eu_id:
                nutrient_row = get_eu_ingredient_nutrition(str(eu_id))
        elif active_source == "slovenian":
            if slovenian_id:
                nutrient_row = get_slovenian_ingredient_nutrition(str(slovenian_id))

        if not nutrient_row:
            details.append({
                "ingredient": ing_name,
                "source": _source_label(active_source),
                "source_nutrition": _source_label(active_source),
                "matched_nutritional_ingredient": None,
                "canonical_food_id": (
                    canonical_food_id if active_source in {"irish", "hungarian"}
                    else eu_id if active_source == "eu"
                    else slovenian_id if active_source == "slovenian"
                    else None
                ),
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
                "match_confidence": match_confidence,
                "match_reason": match_reason,
            })
            continue

        meta = nutrient_row

        matched_name = (
            meta.get("Food Name")
            or meta.get("food_name")
            or vector_metadata.get("title")
            or match.get("document")
            or "—"
        )

        if active_source in {"irish", "hungarian"}:
            # Pull macro values per 100g with safe fallbacks
            if active_source == "irish":
                protein_per_100g = _to_float(meta.get(PROTEIN_KEY, 0.0))
                carbs_per_100g = _to_float(meta.get(CARB_KEY, 0.0))
                fat_per_100g = _to_float(meta.get(FAT_KEY, 0.0))
                sugars_per_100g = _to_float(meta.get(SUGARS_KEY, 0.0))
                saturated_fat_per_100g = _to_float(meta.get(SATURATED_FAT_KEY, 0.0))
                sodium_per_100g_mg = _to_float(meta.get(SODIUM_KEY, 0.0))
                fibre_per_100g = _to_float(meta.get("Fibre (g)", meta.get("Fiber (g)", 0.0)))
                energy_kcal_per_100g = _to_float(meta.get(ENERGY_KCAL_KEY), default=0.0)
                energy_kj_per_100g = _to_float(meta.get(ENERGY_KJ_KEY), default=0.0)
            else:
                protein_per_100g = _first_float(meta, HUNGARIAN_PROTEIN_KEYS, default=0.0)
                carbs_per_100g = _first_float(meta, HUNGARIAN_CARB_KEYS, default=0.0)
                fat_per_100g = _first_float(meta, HUNGARIAN_FAT_KEYS, default=0.0)
                sodium_per_100g_mg = _first_float(meta, HUNGARIAN_SODIUM_KEYS, default=0.0)
                energy_kcal_per_100g = _first_float(meta, HUNGARIAN_ENERGY_KCAL_KEYS, default=0.0)
                energy_kj_per_100g = _first_float(meta, HUNGARIAN_ENERGY_KJ_KEYS, default=0.0)
                # These fields are absent from the Hungarian source. Keep the
                # values explicitly empty rather than borrowing another table.
                sugars_per_100g = 0.0
                saturated_fat_per_100g = 0.0
                fibre_per_100g = 0.0

            # Try to read kcal/100g from metadata; if missing, approximate via 4/4/9
            if energy_kcal_per_100g <= 0:
                energy_kcal_per_100g = None

            if not energy_kcal_per_100g:
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
            "source": _source_label(active_source),
            "source_nutrition": _source_label(active_source),
            "matched_nutritional_ingredient": matched_name,
            "canonical_food_id": (
                canonical_food_id if active_source in {"irish", "hungarian"}
                else eu_id if active_source == "eu"
                else slovenian_id if active_source == "slovenian"
                else None
            ),
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
            "match_confidence": match_confidence,
            "match_reason": match_reason,
        })

        total_energy_kcal += energy_kcal

    result: Dict = {
        "title": title,
        "details": details,
        "source": source,
        "source_nutrition": _source_label(source_normalized),
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

    # Clean keys (no source suffix) — used for consistent postgres storage
    result["clean_totals"] = {k: float(v) for k, v in totals_map.items()}
    if serves_value:
        result["clean_totals_per_serving"] = {
            k: float(v) / float(serves_value) for k, v in totals_map.items()
        }

    return result


def Nutrition_Node(state: RecipeState) -> RecipeState:
    """
    Node to compute nutrition via Elasticsearch, scale by weight/serves, store totals and details in state.
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

    res = nutritional_tool_vector.invoke({
        "title": state.title or "Untitled Recipe",
        "ingredient_names": ingredient_names,
        "weights": weights,
        "min_similarity": state.min_similarity if state.min_similarity is not None else 0.7,
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
            f"\n[Nutrition_Node] Computed (Elasticsearch) for recipe "
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
