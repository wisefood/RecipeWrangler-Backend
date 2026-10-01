# Purpose: Compute nutrition totals from ingredient weights via Elasticsearch matches.

import logging
import os
import re
from typing import Dict, List, Optional

from langchain.tools import tool

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.repositories.postgres_nutrition import (
    get_eu_ingredient_nutrition,
    get_hungarian_ingredient_nutrition,
    get_irish_ingredient_nutrition,
    get_slovenian_ingredient_nutrition,
)
from recipe_wrangler.tools.nutrition_match import (
    _CONCENTRATE_FORM_MARKERS,
    _tokens,
    best_nutrition_match,
    food_class,
)

# Whether a `weak`-confidence ingredient match may contribute nutrients.
# Off means the old behaviour: any nearest neighbour is used, however
# implausible. Escape hatch only — leave it on.
REJECT_WEAK_NUTRITION_MATCHES = (
    os.getenv('NUTRITION_REJECT_WEAK_MATCHES', '1').strip().lower()
    not in {'0', 'false', 'no'}
)


def _contextual_match_name(
    match_name: str,
    identity_name: str,
    measurement: object,
    weight_g: float,
) -> str:
    """Disambiguate the common bare `pepper` spice/produce collision."""
    if (
        str(match_name).strip().casefold() != "pepper"
        or str(identity_name).strip().casefold() != "pepper"
    ):
        return match_name
    measure = str(measurement or "").strip().casefold()
    if re.search(r"\b(?:tsp|teaspoons?|pinch|dash|taste|season|ground)\b", measure):
        return "black pepper"
    if re.search(r"\b(?:small|medium|large|red|green|yellow|capsicum|bell)\b", measure):
        return "sweet pepper"
    if re.search(r"\d\s*(?:g|kg|oz|ounces?|lb|pounds?)\b", measure):
        return "sweet pepper"
    if re.fullmatch(r"\d+(?:\.\d+)?", measure) and weight_g >= 20.0:
        return "sweet pepper"
    return "black pepper"

logger = logging.getLogger(__name__)

SOURCE_NUTRITION = "Irish Composition Table"
SOURCE_NUTRITION_EU = "EU"

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
HUNGARIAN_SUGAR_KEYS = ("Sugar (g)",)
HUNGARIAN_SATURATED_FAT_KEYS = ("Saturated Fat (g)",)
HUNGARIAN_FIBER_KEYS = ("Fiber (g)",)

# A stock cube, bouillon powder, or plain salt carries per-100g nutrition for
# the concentrate itself, not for the water/dish it seasons. An upstream
# parsing bug (fixing the parser is a separate, larger piece of work — see
# `data/analysis/nutrition_curation/PLAN.md`) can attach the *whole line's*
# weight to that concentrate match — e.g. "850g water and 1 vegetable stock
# cube" resolving to 850g of stock cube nutrition instead of ~10g. That turns
# a normal soup into ~47,600mg of sodium per serving. This is a backstop, not
# a fix for the parser: any match whose per-100g sodium is concentrate-level
# AND whose assigned weight is implausible for a concentrate gets capped to a
# typical concentrate serving before it's scaled into the totals.
# ponytail: a flat cap, not a per-food-type portion table; revisit if a real
# compound-line splitter (Step 3 of the plan) lands and makes this redundant.
_CONCENTRATE_SODIUM_MG_PER_100G = 3000.0
_CONCENTRATE_IMPLAUSIBLE_WEIGHT_G = 200.0
_CONCENTRATE_CAPPED_WEIGHT_G = 15.0


def _capped_concentrate_weight_g(
    weight_g: float, sodium_per_100g_mg: float, matched_name: str
) -> tuple[float, bool]:
    # Density alone is not enough: real foods clear this sodium density and
    # legitimately appear at a bulk weight — anchovies (~3500mg/100g), fish
    # sauce (~7000), soy sauce (~5500), cured meats. Found via the plausibility
    # audit: "dried noodles" matched to "Soup, chicken noodle, dried" would
    # otherwise get its legitimate 250-375g crushed to 15g. Require the
    # matched food's own name to say it's a concentrate/dry-mix product.
    if not (_CONCENTRATE_FORM_MARKERS & set(_tokens(matched_name))):
        return weight_g, False
    if weight_g > _CONCENTRATE_IMPLAUSIBLE_WEIGHT_G and sodium_per_100g_mg >= _CONCENTRATE_SODIUM_MG_PER_100G:
        return _CONCENTRATE_CAPPED_WEIGHT_G, True
    return weight_g, False


# A "salt and pepper" / "black pepper" seasoning line is written as "to
# taste", but a weight-parsing bug can still attach a real gram figure to it
# (17% of the 2026-09-16 plausibility audit's flagged lines: e.g. "salt and
# pepper" at 720g across 7 lines, "black pepper" at 750g, "4 cups" black
# pepper parsed as 436g). food_class() alone under-catches this: "pepper" is
# deliberately left ambiguous there (it also means bell pepper/capsicum), so
# gate on an explicit phrase list for pepper and salt specifically, plus
# food_class for the unambiguous herb/spice names (cinnamon, cumin, ...).
# ponytail: flat cap, not a per-spice portion table; same shape as the
# concentrate cap above.
_SEASONING_NAME_PHRASES = ("salt", "black pepper", "white pepper", "peppercorn", "zest")
_SEASONING_IMPLAUSIBLE_WEIGHT_G = 30.0
_SEASONING_CAPPED_WEIGHT_G = 5.0
# Matches ingredient_weight_tool.py's TO_TASTE_MIN_GRAMS / BLANK_DEFAULT_GRAMS
# policy (2026-09-25 product decision: a line with no stated amount gets a
# negligible-but-nonzero weight, not "0 g"). That policy only fires when the
# weight tool left the weight at exactly 0 -- it does not correct an
# already-wrong nonzero guess (the 120g-for-unquantified-pepper case this
# cap exists for). When the recipe line truly gave no quantity at all, honor
# the 0.5g policy here instead of the flat 5g fallback for "a number was
# given but it's an implausible one".
_SEASONING_BLANK_MEASUREMENT_GRAMS = 0.5

# Garnish seeds/nuts (sprinkled, not a bulk ingredient) are a separate class
# from pure spices: a real garnish amount is closer to a tablespoon (~10g)
# than a pinch, but the parser can still hand them an unquantified-line
# weight in the hundreds or thousands of grams (found: "sesame or pumpkin
# seeds", no stated quantity, parsed as 1000g, on a recipe whose other
# unquantified lines are explicitly "to garnish"). Only fires when the line
# truly stated no quantity -- a recipe that actually says "1 cup pumpkin
# seeds" is a real bulk ingredient and must not be capped.
_GARNISH_SEED_TOKENS = {"sesame", "pumpkin", "sunflower", "poppy", "chia", "flax", "flaxseed"}
_GARNISH_SEED_IMPLAUSIBLE_WEIGHT_G = 30.0
_GARNISH_SEED_BLANK_MEASUREMENT_GRAMS = 10.0


_MEASUREMENT_NOT_PROVIDED = object()


def _capped_seasoning_weight_g(
    weight_g: float,
    ingredient_name: str,
    measurement: object = _MEASUREMENT_NOT_PROVIDED,
    matched_name: str = "",
) -> tuple[float, bool]:
    if weight_g <= _SEASONING_IMPLAUSIBLE_WEIGHT_G:
        return weight_g, False
    name = str(ingredient_name or "").lower()
    # "salt-free vegetable stock" contains the substring "salt" but names a
    # stock, not a seasoning -- that line's own implausible weight is the
    # concentrate cap's job, not this one's. Guard against the phrase check
    # firing on a different food that merely mentions salt as a qualifier.
    if {"stock", "broth", "sauce", "gravy"} & set(_tokens(name)):
        return weight_g, False
    is_seasoning = any(phrase in name for phrase in _SEASONING_NAME_PHRASES) or (
        food_class(name) == "spice_herb"
    )
    if not is_seasoning:
        # Bare "pepper" is ambiguous between the Piper nigrum spice and the
        # capsicum vegetable (same ambiguity nutrition_match.py's pepper
        # guard resolves for composition matching) -- the ingredient's own
        # name doesn't say which, but what it actually matched to does.
        # "Lemon zest" at 58g revealed the same class of gap: implausible
        # parser weight on a flavoring-amount ingredient the name-phrase
        # list didn't anticipate.
        matched = str(matched_name or "").lower()
        matched_tokens = set(_tokens(matched))
        is_spice_pepper = (
            "pepper" in matched_tokens
            and {"black", "white"} & matched_tokens
            and not ({"capsicum", "sweet", "bell", "chilli", "chili"} & matched_tokens)
        )
        is_seasoning = is_spice_pepper or "zest" in matched_tokens
    if is_seasoning:
        # Distinguish "caller didn't pass a measurement" (callers that only
        # care about the general cap, e.g. tests) from "the recipe line
        # genuinely stated no quantity" (measurement == "") -- only the
        # latter gets the stricter to-taste default.
        if measurement is not _MEASUREMENT_NOT_PROVIDED and not str(measurement or "").strip():
            return _SEASONING_BLANK_MEASUREMENT_GRAMS, True
        return _SEASONING_CAPPED_WEIGHT_G, True
    # Garnish seeds: only correct the case the recipe line gave no quantity
    # at all. A stated amount ("1 cup pumpkin seeds") is a real bulk
    # ingredient, not a garnish sprinkle, and must be left alone.
    if (
        measurement is not _MEASUREMENT_NOT_PROVIDED
        and not str(measurement or "").strip()
        and weight_g > _GARNISH_SEED_IMPLAUSIBLE_WEIGHT_G
        and _GARNISH_SEED_TOKENS & set(_tokens(str(matched_name or "").lower()))
    ):
        return _GARNISH_SEED_BLANK_MEASUREMENT_GRAMS, True
    return weight_g, False


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
    ingredient_identity_names: Optional[List[str]] = None,
    measurements: Optional[List[str]] = None,
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

    identity_names = (
        ingredient_identity_names
        if isinstance(ingredient_identity_names, list)
        and len(ingredient_identity_names) == len(ingredient_names)
        else ingredient_names
    )
    measurement_values = (
        measurements
        if isinstance(measurements, list) and len(measurements) == len(ingredient_names)
        else [""] * len(ingredient_names)
    )
    for ing_name, identity_name, weight_g, measurement in zip(
        ingredient_names, identity_names, weights, measurement_values
    ):
        match_name = _contextual_match_name(
            ing_name, identity_name, measurement, float(weight_g)
        )
        m = best_nutrition_match(
            match_name,
            source_normalized,
            float(min_similarity),
            identity_name=identity_name,
        )
        match = m.get("match")
        active_source = m.get("source_key") or source_normalized
        match_confidence = m.get("confidence")
        match_reason = m.get("reason")
        nutrition_match_note = m.get("nutrition_match_note")
        intentionally_ignored = bool(m.get("intentionally_ignored"))
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
                "nutrition_match_note": nutrition_match_note,
                "nutrition_intentionally_ignored": intentionally_ignored,
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
                "nutrition_match_note": nutrition_match_note,
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
                sugars_per_100g = _first_float(meta, HUNGARIAN_SUGAR_KEYS, default=0.0)
                saturated_fat_per_100g = _first_float(
                    meta, HUNGARIAN_SATURATED_FAT_KEYS, default=0.0
                )
                fibre_per_100g = _first_float(meta, HUNGARIAN_FIBER_KEYS, default=0.0)

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

        original_weight_g = float(weight_g)
        weight_g, weight_capped = _capped_concentrate_weight_g(
            original_weight_g, float(sodium_per_100g_mg), matched_name
        )
        if not weight_capped:
            weight_g, weight_capped = _capped_seasoning_weight_g(
                weight_g, ing_name, measurement, matched_name
            )
        if weight_capped:
            logger.info(
                "nutrition: capping implausible weight %r %.0fg -> %.0fg",
                ing_name, original_weight_g, weight_g,
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
            "weight_capped": weight_capped,
            "original_weight_g": original_weight_g if weight_capped else None,
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
            "nutrition_match_note": nutrition_match_note,
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
    match_names = (
        list(state.ingredient_match_names[:n])
        if len(state.ingredient_match_names or []) >= n
        else ingredient_names
    )

    source = (
        getattr(state, "nutrition_source", None)
        or getattr(state, "nutritional_source", None)
        or getattr(state, "source", None)
        or "irish"
    )

    res = nutritional_tool_vector.invoke({
        "title": state.title or "Untitled Recipe",
        "ingredient_names": match_names,
        "ingredient_identity_names": ingredient_names,
        "weights": weights,
        "measurements": list(state.measurements[:n]) if state.measurements else None,
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
