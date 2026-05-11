# Purpose: Compute nutrition totals from ingredient weights via Chroma matches.

import re
from typing import Dict, List, Optional

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.repositories.postgres_nutrition import (
    get_hungarian_ingredient_nutrition,
    get_irish_ingredient_nutrition,
    get_usda_ingredient_nutrition,
)
from recipe_wrangler.repositories.chroma_matchers import (
    query_hungarian_nutrition_candidates,
    query_irish_nutrition_candidates,
    query_usda_nutrition_candidates,
)
from recipe_wrangler.tools.nutrition_match import best_nutrition_match

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
SOURCE_NUTRITION_HUNGARIAN = "Hungarian Composition Table"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_TOKENS = {
    "fresh", "raw", "cooked", "dried", "whole", "large", "small", "medium",
    "cup", "cups", "tbsp", "tsp", "tablespoon", "teaspoon",
    "white", "red", "green", "black",
}
_REGIONAL_USDA_LEXICAL_FALLBACK_TOKENS = {
    "chickpea",
    "chickpeas",
    "garbanzo",
    "tahini",
    "wine",
    "parsley",
}
_ZERO_IF_UNRELATED_TOKENS = {"stock", "broth"}
_USDA_MISMATCH_GUARDS = {
    "chicken": {"fat", "skin", "drippings"},
    "pepper": {"sauce", "hot", "ready", "serve", "ready-to-serve"},
}
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
    USDA nutrients are stored as nested objects like {"value": 12.3, "unit": "g"}.
    Irish values are plain numeric-like strings.
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
    if source_key == "usda":
        return SOURCE_NUTRITION_USDA
    if source_key == "hungarian":
        return SOURCE_NUTRITION_HUNGARIAN
    return SOURCE_NUTRITION


def _best_usda_match(
    ingredient_name: str,
    min_similarity: float,
) -> tuple[Optional[dict], Optional[float], Optional[float]]:
    usda_matches = query_usda_nutrition_candidates(ingredient_name) or []
    usda_match = _select_usda_match(ingredient_name, usda_matches)
    if not usda_match:
        return None, None, None
    usda_distance = usda_match.get("distance", None)
    usda_similarity = None if usda_distance is None else (1.0 - float(usda_distance))
    if usda_similarity is not None and usda_similarity < float(min_similarity):
        return None, usda_distance, usda_similarity
    return usda_match, usda_distance, usda_similarity


def _usda_gap_nutrients(ingredient_name: str, min_similarity: float) -> dict:
    """Return the nutrients that regional DBs commonly lack, sourced from USDA.

    Used to fill sugars, saturated fat, and fibre for Hungarian, and any of
    those fields that are null/zero in the Irish table. Returns zeros on failure.
    """
    usda_match, _, _ = _best_usda_match(ingredient_name, min_similarity)
    if not usda_match:
        return {"sugars_per_100g": 0.0, "saturated_fat_per_100g": 0.0, "fibre_per_100g": 0.0}
    usda_id = (usda_match.get("metadata") or {}).get("usda_id")
    nutrient_row = get_usda_ingredient_nutrition(str(usda_id)) if usda_id else None
    if not nutrient_row:
        return {"sugars_per_100g": 0.0, "saturated_fat_per_100g": 0.0, "fibre_per_100g": 0.0}
    nutrients = nutrient_row.get("nutrients") or {}
    return {
        "sugars_per_100g": _nutrient_value(
            nutrients.get("Sugars, total including NLEA", nutrients.get("Sugars, total")), 0.0
        ),
        "saturated_fat_per_100g": _nutrient_value(
            nutrients.get("Fatty acids, total saturated"), 0.0
        ),
        "fibre_per_100g": _nutrient_value(nutrients.get("Fiber, total dietary"), 0.0),
    }


def _tokenize(text: object) -> set[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return set()
    return {tok for tok in _TOKEN_RE.findall(raw) if tok and tok not in _STOP_TOKENS}


def _candidate_name(match: dict) -> str:
    meta = match.get("metadata") or {}
    return str(
        meta.get("food_name")
        or meta.get("Food Name")
        or meta.get("title")
        or match.get("document")
        or ""
    ).strip()


def _meaningful_overlap(left: set[str], right: set[str]) -> set[str]:
    return {token for token in left & right if token not in _STOP_TOKENS}


def _select_usda_match(ingredient_name: str, matches: List[dict]) -> Optional[dict]:
    if not matches:
        return None

    query_tokens = _tokenize(ingredient_name)
    best_match = None
    best_score = float("-inf")

    for match in matches:
        if not isinstance(match, dict):
            continue
        candidate_name = _candidate_name(match)
        candidate_tokens = _tokenize(candidate_name)
        distance = match.get("distance")
        similarity = 0.0 if distance is None else (1.0 - float(distance))

        overlap = len(query_tokens & candidate_tokens)
        lexical_boost = 0.0
        if query_tokens:
            lexical_boost = 0.25 * (overlap / len(query_tokens))

        penalty = 0.0
        for token, banned in _USDA_MISMATCH_GUARDS.items():
            if token in query_tokens and not (query_tokens & banned) and (candidate_tokens & banned):
                penalty -= 0.35

        score = similarity + lexical_boost + penalty
        if score > best_score:
            best_score = score
            best_match = match

    return best_match


def _best_usda_lexical_match(
    ingredient_name: str,
    min_similarity: float = 0.6,
) -> tuple[Optional[dict], Optional[float], Optional[float]]:
    query_tokens = _tokenize(ingredient_name)
    matches = query_usda_nutrition_candidates(ingredient_name) or []
    best = None
    best_distance = None
    best_similarity = None
    best_overlap = 0

    for match in matches:
        candidate_name = _candidate_name(match)
        candidate_tokens = _tokenize(candidate_name)
        overlap = len(_meaningful_overlap(query_tokens, candidate_tokens))
        if overlap <= 0:
            continue
        distance = match.get("distance", None)
        similarity = None if distance is None else (1.0 - float(distance))
        if similarity is not None and similarity < float(min_similarity):
            continue
        candidate_sort_distance = float(distance) if distance is not None else float("inf")
        if query_tokens & {"chickpea", "chickpeas", "garbanzo"}:
            candidate_name_l = candidate_name.lower()
            if "raw" in candidate_name_l and not any(
                token in candidate_name_l for token in ("canned", "cooked")
            ):
                candidate_sort_distance += 0.08
            if any(token in candidate_name_l for token in ("canned", "cooked")):
                candidate_sort_distance -= 0.04

        best_sort_distance = (
            float(best_distance) if best_distance is not None else float("inf")
        )
        if best and query_tokens & {"chickpea", "chickpeas", "garbanzo"}:
            best_name_l = _candidate_name(best).lower()
            if "raw" in best_name_l and not any(
                token in best_name_l for token in ("canned", "cooked")
            ):
                best_sort_distance += 0.08
            if any(token in best_name_l for token in ("canned", "cooked")):
                best_sort_distance -= 0.04

        if overlap > best_overlap or (
            overlap == best_overlap
            and candidate_sort_distance < best_sort_distance
        ):
            best = match
            best_distance = distance
            best_similarity = similarity
            best_overlap = overlap

    return best, best_distance, best_similarity


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
            raise ValueError("nutritional_tool_chroma: 'serves' must be numeric.") from exc
        if serves_value <= 0:
            serves_value = None

    source_normalized = (source or "irish").strip().lower()

    # Select query function based on source
    def _query_nutrition(name: str):
        if source_normalized == "irish":
            return query_irish_nutrition_candidates(name)
        if source_normalized == "usda":
            return query_usda_nutrition_candidates(name)
        if source_normalized == "hungarian":
            return query_hungarian_nutrition_candidates(name)
        return query_irish_nutrition_candidates(name)

    for ing_name, weight_g in zip(ingredient_names, weights):
        m = best_nutrition_match(ing_name, source_normalized, float(min_similarity))
        match = m.get("match")
        active_source = m.get("source_key") or source_normalized
        match_confidence = m.get("confidence")
        match_reason = m.get("reason")
        distance = None if match is None else match.get("distance")
        similarity = m.get("similarity")

        if match is None:
            details.append({
                "ingredient": ing_name,
                "source": _source_label(active_source),
                "source_nutrition": _source_label(active_source),
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
                "similarity": similarity,
                "match_confidence": match_confidence,
                "match_reason": match_reason,
            })
            continue

        chroma_meta = match.get("metadata") or {}
        canonical_food_id = chroma_meta.get("canonical_food_id")
        usda_id = chroma_meta.get("usda_id")
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
        else:
            if usda_id:
                nutrient_row = get_usda_ingredient_nutrition(str(usda_id))

        if (
            active_source == "hungarian"
            and not nutrient_row
        ):
            usda_match, usda_distance, usda_similarity = _best_usda_match(
                ing_name, float(min_similarity)
            )
            if usda_match is not None:
                match = usda_match
                chroma_meta = match.get("metadata") or {}
                usda_id = chroma_meta.get("usda_id")
                if usda_id:
                    nutrient_row = get_usda_ingredient_nutrition(str(usda_id))
                    if nutrient_row:
                        active_source = "usda"
                        distance = usda_distance
                        similarity = usda_similarity

        if not nutrient_row:
            details.append({
                "ingredient": ing_name,
                "source": _source_label(active_source),
                "source_nutrition": _source_label(active_source),
                "matched_nutritional_ingredient": None,
                "canonical_food_id": (
                    canonical_food_id if active_source in {"irish", "hungarian"} else usda_id
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
            or chroma_meta.get("title")
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
                # Fill any missing fields from USDA
                if sugars_per_100g == 0.0 or saturated_fat_per_100g == 0.0 or fibre_per_100g == 0.0:
                    usda_gap = _usda_gap_nutrients(ing_name, float(min_similarity))
                    if sugars_per_100g == 0.0:
                        sugars_per_100g = usda_gap["sugars_per_100g"]
                    if saturated_fat_per_100g == 0.0:
                        saturated_fat_per_100g = usda_gap["saturated_fat_per_100g"]
                    if fibre_per_100g == 0.0:
                        fibre_per_100g = usda_gap["fibre_per_100g"]
            else:
                protein_per_100g = _first_float(meta, HUNGARIAN_PROTEIN_KEYS, default=0.0)
                carbs_per_100g = _first_float(meta, HUNGARIAN_CARB_KEYS, default=0.0)
                fat_per_100g = _first_float(meta, HUNGARIAN_FAT_KEYS, default=0.0)
                sodium_per_100g_mg = _first_float(meta, HUNGARIAN_SODIUM_KEYS, default=0.0)
                energy_kcal_per_100g = _first_float(meta, HUNGARIAN_ENERGY_KCAL_KEYS, default=0.0)
                energy_kj_per_100g = _first_float(meta, HUNGARIAN_ENERGY_KJ_KEYS, default=0.0)
                # Sugars, saturated fat, fibre not in Hungarian DB — fill from USDA
                usda_gap = _usda_gap_nutrients(ing_name, float(min_similarity))
                sugars_per_100g = usda_gap["sugars_per_100g"]
                saturated_fat_per_100g = usda_gap["saturated_fat_per_100g"]
                fibre_per_100g = usda_gap["fibre_per_100g"]

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
                canonical_food_id if active_source in {"irish", "hungarian"} else usda_id
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
