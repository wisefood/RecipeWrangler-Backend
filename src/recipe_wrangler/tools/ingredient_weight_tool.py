# Purpose: Estimate ingredient weights (grams) using USDA portion/weight data.

import csv
import io
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Tuple

import re
from langchain.tools import tool

from recipe_wrangler.tools.ingredient_weight_llm_tool import ingredient_weight_llm_tool
from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.chroma_client import get_chroma_client
from recipe_wrangler.utils.get_embeddings import get_embeddings
from recipe_wrangler.utils.usda_nutrients_v1 import canonical_name_to_usda, usda_id_to_link
from recipe_wrangler.utils.weigh_calculation_usda_ import (
    find_weight_match_by_name,
    match_portion,
    weight_from_density_fallback,
    weight_from_ingredient,
)


def _as_list(x: Any) -> list:
    if x is None:
        return []
    # Handle pandas/numpy NaN
    try:
        import math
        if isinstance(x, float) and math.isnan(x):
            return []
    except Exception:
        pass
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            import ast
            val = ast.literal_eval(x)
            if isinstance(val, list):
                return val
        except Exception:
            return [s.strip() for s in x.split(",") if s.strip()]
        return [x]
    try:
        return list(x)
    except Exception:
        return [x]


_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<qty>[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?(?:\s*-\s*[0-9]+(?:\.[0-9]+)?)?)\s*(?P<unit>.*)$"
)

_RANGE_QTY_RE = r"[0-9]+(?:\.[0-9]+)?(?:\s+[0-9]+/[0-9]+|/[0-9]+)?"
_RANGE_MEASUREMENT_RE = re.compile(
    rf"^\s*(?P<q1>{_RANGE_QTY_RE})\s*(?:to|-|–|—)\s*(?P<q2>{_RANGE_QTY_RE})\s*(?P<rest>.*)$"
)

_UNICODE_FRACTIONS = {
    "½": "1/2",
    "⅓": "1/3",
    "⅔": "2/3",
    "¼": "1/4",
    "¾": "3/4",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}

_WORD_QUANTITIES = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "half": 0.5,
    "quarter": 0.25,
    "couple": 2.0,
    "few": 3.0,
}

_COUNTABLE_NOUNS = {
    "clove": "clove",
    "cloves": "clove",
    "sprig": "sprig",
    "sprigs": "sprig",
    "leaf": "leaf",
    "leaves": "leaf",
    "stalk": "stalk",
    "stalks": "stalk",
    "stick": "stick",
    "sticks": "stick",
    "slice": "slice",
    "slices": "slice",
    "piece": "piece",
    "pieces": "piece",
    "bunch": "bunch",
    "bunches": "bunch",
    "egg": "egg",
    "eggs": "egg",
    "avocado": "avocado",
    "avocados": "avocado",
    "tomato": "tomato",
    "tomatoes": "tomato",
    "apple": "apple",
    "apples": "apple",
    "banana": "banana",
    "bananas": "banana",
    "onion": "onion",
    "onions": "onion",
    "carrot": "carrot",
    "carrots": "carrot",
    "potato": "potato",
    "potatoes": "potato",
}

_MASS_UNITS = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mg": 0.001,
    "milligram": 0.001,
    "milligrams": 0.001,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}

_UNIT_ALIASES = {
    "c": "cup",
    "cups": "cup",
}

_UNIT_STOPWORDS = {
    "of",
    "fresh",
    "ripe",
    "large",
    "medium",
    "small",
    "big",
    "whole",
    "raw",
}

HERB_SPICE_FOOD_GROUP = "Spices and Herbs"
HERB_MISSING_UNIT_FALLBACK_UNIT = "pinch"
HERB_MISSING_UNIT_FALLBACK_GRAMS = 0.3
VEGETABLES_FOOD_GROUP = "Vegetables and Vegetable Products"
LLM_UNIT_GRAMS_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "fallbacks"
    / "ingredient_unit_grams.csv"
)
FDA_UNIT_GRAMS_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "fallbacks"
    / "ingredient_unit_grams_fda.csv"
)
LLM_UNIT_GRAMS_MATCH_TYPE = "llm_ingredient_unit_grams_fallback"
LLM_UNIT_GRAMS_NOTE = "LLM fallback from ingredient_unit_grams.csv"
FDA_UNIT_GRAMS_MATCH_TYPE = "fda_reference_portion_fallback"
FDA_UNIT_GRAMS_NOTE = "FDA reference portion fallback from ingredient_unit_grams_fda.csv"
RECIPE1M_LLM_WEIGHT_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "recipe1m"
    / "recipe1m-unmatched-ingredient-weights-llm.csv"
)
RECIPE1M_LLM_WEIGHT_MATCH_TYPE = "llm_weight_fallback"
RECIPE1M_LLM_WEIGHT_NOTE = "LLM fallback from recipe1m-unmatched-ingredient-weights-llm.csv"
RECIPE1M_LLM_PORTION_FALLBACK_CSV_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "processed"
    / "recipe1m"
    / "food_weights_updated.csv"
)
RECIPE1M_LLM_PORTION_FALLBACK_MATCH_TYPE = "llm_portion_per_unit_fallback"
RECIPE1M_LLM_PORTION_FALLBACK_NOTE = "LLM per-unit fallback from food_weights_updated.csv"
HERB_SPICE_HINT_TOKENS = {
    "herb",
    "spice",
    "seasoning",
    "powder",
    "pepper",
    "basil",
    "oregano",
    "thyme",
    "rosemary",
    "cumin",
    "paprika",
    "cinnamon",
    "nutmeg",
    "clove",
    "dill",
    "parsley",
    "sage",
    "turmeric",
    "chili",
    "chilli",
    "coriander",
    "tarragon",
    "mint",
    "bay leaf",
    "cardamom",
}


USDA_LINKS_EMBED_COLLECTIONS = tuple(
    c.strip()
    for c in os.getenv(
        "USDA_LINKS_EMBED_COLLECTIONS",
        "nutritional_ingredients_usda,usda_ingredients_canonical",
    ).split(",")
    if c.strip()
)
USDA_LINKS_EMBED_QUERY_K = int(os.getenv("USDA_LINKS_EMBED_QUERY_K", "3"))
USDA_LINKS_EMBED_MAX_DISTANCE = float(
    os.getenv("USDA_LINKS_EMBED_MAX_DISTANCE", "0.45")
)
USDA_LINKS_EMBED_MIN_CONFIDENCE = float(
    os.getenv("USDA_LINKS_EMBED_MIN_CONFIDENCE", "0.65")
)


@lru_cache(maxsize=1)
def _get_usda_links_collections() -> list:
    collections = []
    try:
        client = get_chroma_client()
    except Exception:
        return collections
    for name in USDA_LINKS_EMBED_COLLECTIONS:
        try:
            collections.append(client.get_collection(name=name))
        except Exception:
            continue
    return collections


@lru_cache(maxsize=2048)
def _embedding_usda_link(name: str, unit: Optional[str] = None) -> Optional[dict]:
    query = str(name or "").strip()
    if not query:
        return None

    collections = _get_usda_links_collections()
    if not collections:
        return None

    try:
        query_vec = get_embeddings(query)
    except Exception:
        return None

    all_candidates: list[dict] = []
    for collection in collections:
        try:
            results = collection.query(
                query_embeddings=[query_vec],
                n_results=max(1, USDA_LINKS_EMBED_QUERY_K),
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            continue

        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        for doc, meta, dist in zip(docs, metas, dists):
            meta = meta or {}
            usda_id = str(meta.get("usda_id") or "").strip()
            if not usda_id:
                continue

            distance = None
            if dist is not None:
                try:
                    distance = float(dist)
                except (TypeError, ValueError):
                    distance = None

            if (
                distance is not None
                and USDA_LINKS_EMBED_MAX_DISTANCE >= 0.0
                and distance > USDA_LINKS_EMBED_MAX_DISTANCE
            ):
                continue

            similarity = None
            if distance is not None:
                try:
                    # Distance->similarity is collection-metric dependent; keep
                    # this as informational only, never as hard filter.
                    similarity = 1.0 - distance
                except (TypeError, ValueError):
                    similarity = None

            if (
                similarity is not None
                and USDA_LINKS_EMBED_MIN_CONFIDENCE >= 0.0
                and similarity < USDA_LINKS_EMBED_MIN_CONFIDENCE
            ):
                continue

            canonical = meta.get("usda_food_label") or meta.get("name") or doc

            candidate = {
                "usda_id": usda_id,
                "canonical_id": meta.get("canonical_id"),
                "canonical": canonical,
                "usda_food_label": meta.get("usda_food_label"),
                "food_group_id": meta.get("food_group_id"),
                "food_group": meta.get("food_group"),
                "distance": distance,
                "similarity": similarity,
                "match_source": "embedding",
                "match_collection": getattr(collection, "name", None),
            }

            # Prefer candidates that can support the parsed non-mass unit,
            # then rank by minimum distance.
            supports_unit = True
            if unit and unit.strip().lower() not in _MASS_UNITS:
                if not match_portion(usda_id, unit, name=query):
                    supports_unit = False
            candidate["_supports_unit"] = supports_unit
            all_candidates.append(candidate)

    if not all_candidates:
        return None

    prefer_unit = bool(unit and unit.strip().lower() not in _MASS_UNITS)
    all_candidates.sort(
        key=lambda c: (
            0 if (c.get("_supports_unit") or not prefer_unit) else 1,
            float(c.get("distance")) if c.get("distance") is not None else float("inf"),
        )
    )

    best = dict(all_candidates[0])
    best.pop("_supports_unit", None)
    return best


def _clean_unit(unit_part: str) -> Optional[str]:
    unit_part = unit_part.strip()
    if not unit_part:
        return None
    tokens = [t.strip(".,") for t in unit_part.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None

    # If any token clearly maps to a known countable noun, prefer it.
    for token in reversed(tokens):
        normalized = _UNIT_ALIASES.get(token, token)
        if normalized in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[normalized]

    if len(tokens) >= 2:
        first_two = " ".join(tokens[:2])
        if first_two in {"fl oz", "fluid ounce", "fluid ounces"}:
            return first_two
    filtered_tokens = [t for t in tokens if t not in _UNIT_STOPWORDS]
    if filtered_tokens:
        first_filtered = _UNIT_ALIASES.get(filtered_tokens[0], filtered_tokens[0])
        if first_filtered in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[first_filtered]
        return first_filtered

    first = _UNIT_ALIASES.get(tokens[0], tokens[0])
    if first in _COUNTABLE_NOUNS:
        return _COUNTABLE_NOUNS[first]
    return first


def _normalize_fraction_text(text: str) -> str:
    for symbol, replacement in _UNICODE_FRACTIONS.items():
        text = text.replace(symbol, replacement)
    return text


def _parse_word_quantity(text: str) -> Tuple[Optional[float], str, bool]:
    tokens = text.split()
    if not tokens:
        return None, text, False
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""
    if first == "half" and second in {"a", "an"}:
        return 0.5, " ".join(tokens[2:]), True
    if first in {"a", "an"} and second == "half":
        return 0.5, " ".join(tokens[2:]), True
    if first in _WORD_QUANTITIES:
        return _WORD_QUANTITIES[first], " ".join(tokens[1:]), True
    return None, text, False


def _split_measurement(measurement: Any) -> Tuple[Optional[str], Optional[str], bool]:
    if measurement is None:
        return None, None, False
    if isinstance(measurement, (int, float)):
        return str(measurement), None, False
    text = _normalize_fraction_text(str(measurement).strip().lower())
    if not text:
        return None, None, False

    # Handle quantity ranges like "1/4 to 1/2 teaspoon ...".
    range_match = _RANGE_MEASUREMENT_RE.match(text)
    if range_match:
        q1 = _parse_quantity_value(range_match.group("q1"))
        q2 = _parse_quantity_value(range_match.group("q2"))
        if q1 is not None and q2 is not None:
            qty = str((q1 + q2) / 2.0)
            unit = _clean_unit(range_match.group("rest") or "")
            return qty, unit, False

    if not re.search(r"\d", text):
        qty_word, remainder, inferred = _parse_word_quantity(text)
        if qty_word is None:
            return None, None, False
        unit = _clean_unit(remainder or "")
        return str(qty_word), unit, inferred

    match = _MEASUREMENT_RE.match(text)
    if not match:
        return None, None, False
    qty = match.group("qty").strip()
    unit = _clean_unit(match.group("unit") or "")
    return qty, unit, False


def _infer_unit_from_name(name: str) -> Optional[str]:
    tokens = re.split(r"[\s,-]+", str(name).strip().lower())
    for token in tokens:
        if token in _COUNTABLE_NOUNS:
            return _COUNTABLE_NOUNS[token]
    return None


def _parse_quantity_value(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = _normalize_fraction_text(str(value)).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if " " in text:
        whole, frac = text.split(" ", 1)
        try:
            return float(whole) + _parse_quantity_value(frac)
        except (TypeError, ValueError):
            return None
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            return float(num) / float(den)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None


def _estimate_grams_from_usda_id(
    name: str,
    qty: str,
    unit: str,
    usda_id: str,
) -> tuple[float, Optional[dict], str]:
    unit_norm = unit.strip().lower()
    if unit_norm in _MASS_UNITS:
        qty_value = _parse_quantity_value(qty)
        if qty_value is None:
            raise ValueError("Invalid quantity value")
        grams = qty_value * _MASS_UNITS[unit_norm]
        return grams, None, "direct_mass"

    portion_match = match_portion(usda_id, unit, name=name)
    if not portion_match:
        density_result = weight_from_density_fallback(
            usda_id=usda_id,
            unit=unit,
            quantity=qty,
            name=name,
        )
        if not density_result:
            raise ValueError("No direct portion match")

        portion_match = {
            "portion_desc": f"{density_result['target_unit']} (density fallback)",
            "grams_per_unit": density_result["grams_per_target_unit"],
            "density_g_per_ml": density_result["density_g_per_ml"],
            "density_candidate_count": density_result["density_candidate_count"],
            "density_source_portion_desc": density_result["source_portion_desc"],
            "density_source_unit": density_result["source_unit"],
            "density_source_unit_ml": density_result["source_unit_ml"],
            "density_source_grams_per_unit": density_result["source_grams_per_unit"],
            "target_unit_ml": density_result["target_unit_ml"],
        }
        return float(density_result["grams"]), portion_match, "density_fallback"

    grams = weight_from_ingredient(
        {"name": name, "usda_id": usda_id, "quantity": qty, "unit": unit}
    )
    return grams, portion_match, "direct"


def _estimate_grams_from_name_portion(
    name: str,
    qty: str,
    unit: str,
) -> tuple[float, dict, Optional[str]]:
    """
    Fallback using the USDA weights index by ingredient name + unit, even when
    the canonical USDA link is unsuitable for the parsed unit.
    """
    qty_value = _parse_quantity_value(qty)
    if qty_value is None:
        raise ValueError("Invalid quantity value")

    matched = find_weight_match_by_name(name=name, unit=unit)
    if not matched:
        raise ValueError("No name-based portion match")

    portion = matched.get("portion") or {}
    grams_per_unit = portion.get("grams_per_unit")
    if grams_per_unit is None:
        grams = portion.get("grams")
        amount = portion.get("amount")
        try:
            if grams is not None and amount not in (None, 0):
                grams_per_unit = float(grams) / float(amount)
        except (TypeError, ValueError, ZeroDivisionError):
            grams_per_unit = None

    if grams_per_unit is None:
        raise ValueError("No grams_per_unit in name-based portion match")

    grams_per_unit = float(grams_per_unit)
    grams = float(qty_value) * grams_per_unit
    portion_match = {
        "portion_desc": portion.get("portion_desc"),
        "grams_per_unit": grams_per_unit,
        "source_food_name": matched.get("food_name"),
        "source": "weight_name_fallback",
    }
    usda_id = matched.get("usda_id")
    return grams, portion_match, (str(usda_id).strip() if usda_id else None)

def _food_group_for_link(link: Optional[dict], usda_id: Optional[str]) -> Optional[str]:
    if isinstance(link, dict):
        group = str(link.get("food_group") or "").strip()
        if group:
            return group
    if usda_id:
        linked = usda_id_to_link(str(usda_id))
        if isinstance(linked, dict):
            group = str(linked.get("food_group") or "").strip()
            if group:
                return group
    return None

def _has_herb_spice_hint(*texts: Optional[str]) -> bool:
    hay = " ".join(str(t or "").strip().lower() for t in texts if t)
    if not hay:
        return False
    return any(token in hay for token in HERB_SPICE_HINT_TOKENS)


def _is_pinch_default_candidate(name: str, food_group: Optional[str], herb_spice_hint: bool) -> bool:
    if herb_spice_hint:
        return True
    group = str(food_group or "").strip().lower()
    if group == HERB_SPICE_FOOD_GROUP.lower():
        return True
    n = str(name or "").strip().lower()
    return any(tok in n for tok in ("salt", "pepper", "seasoning"))


def _normalize_lookup_ingredient(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _singularize_token(token: str) -> str:
    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and not token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _lookup_name_variants(name: str) -> list[str]:
    base = _normalize_lookup_ingredient(name)
    if not base:
        return []
    variants = [base]
    tokens = base.split()
    singular = " ".join(_singularize_token(t) for t in tokens).strip()
    if singular and singular != base:
        variants.append(singular)
    return variants


def _csv_rows_from_path_or_pg(path: Path, pg_name: str) -> list[dict]:
    """Return CSV rows from Postgres."""
    from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data
    return load_pipeline_data(pg_name)


@lru_cache(maxsize=1)
def _load_llm_unit_grams_index() -> dict:
    index_by_name: dict[str, dict[str, Any]] = {}
    index_by_usda_id: dict[str, dict[str, Any]] = {}
    sources = [
        (FDA_UNIT_GRAMS_CSV_PATH, "fda", "ingredient_unit_grams_fda"),
        (LLM_UNIT_GRAMS_CSV_PATH, "llm", "ingredient_unit_grams_llm"),
    ]
    for path, source_tag, pg_name in sources:
        rows = _csv_rows_from_path_or_pg(path, pg_name)
        if not rows:
            continue
        for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue
            grams_raw = str(row.get("grams") or "").strip()
            if not grams_raw or grams_raw.upper() == "NA":
                continue
            try:
                grams = float(grams_raw)
            except ValueError:
                continue
            unit = str(row.get("unit") or row.get("reference_unit") or "").strip()
            payload = {
                "ingredient": ingredient,
                "unit": unit,
                "grams_per_unit": grams,
                "source": source_tag,
                "source_file": str(path),
                "usda_id": str(row.get("usda_id") or "").strip() or None,
            }
            for key in _lookup_name_variants(ingredient):
                index_by_name.setdefault(key, payload)
            usda_id = str(payload.get("usda_id") or "").strip()
            if usda_id:
                # Prefer earlier sources in `sources` order (FDA before LLM).
                index_by_usda_id.setdefault(usda_id, payload)
    return {"by_name": index_by_name, "by_usda_id": index_by_usda_id}


def _lookup_llm_unit_grams(name: str, usda_id: Optional[str] = None) -> Optional[dict]:
    index = _load_llm_unit_grams_index()
    by_name = index.get("by_name", {})
    for key in _lookup_name_variants(name):
        found = by_name.get(key)
        if found:
            return found
    usda_id_norm = str(usda_id or "").strip()
    if usda_id_norm:
        by_usda_id = index.get("by_usda_id", {})
        found = by_usda_id.get(usda_id_norm)
        if found:
            return found
    return None


def _measurement_signature(
    measurement: Any,
    parsed_qty: Optional[str] = None,
    parsed_unit: Optional[str] = None,
) -> Optional[tuple[float, str]]:
    qty = parsed_qty
    unit = parsed_unit
    if qty is None and unit is None:
        qty, unit, _ = _split_measurement(measurement)
    qty_value = _parse_quantity_value(qty)
    if qty_value is None:
        return None
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None
    return (round(float(qty_value), 6), str(unit_norm))


@lru_cache(maxsize=1)
def _load_recipe1m_llm_weight_index() -> dict:
    index: dict[str, dict[str, Any]] = {}
    rows = _csv_rows_from_path_or_pg(RECIPE1M_LLM_WEIGHT_CSV_PATH, "recipe1m_unmatched_ingredient_weights_llm")
    if not rows:
        return index

    for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue

            llm_error = str(row.get("llm_error") or "").strip()
            grams_raw = str(row.get("llm_weight_grams") or "").strip()
            if llm_error or not grams_raw:
                continue
            try:
                grams = float(grams_raw)
            except ValueError:
                continue
            if grams <= 0:
                continue

            sample_measurement = str(row.get("sample_measurement") or "").strip()
            row_qty = str(row.get("parsed_quantity") or "").strip() or None
            row_unit = str(row.get("parsed_unit") or "").strip() or None
            parsed_signature = _measurement_signature(
                measurement=None,
                parsed_qty=row_qty,
                parsed_unit=row_unit,
            )
            sample_signature = _measurement_signature(sample_measurement)

            payload = {
                "ingredient": ingredient,
                "sample_measurement": sample_measurement,
                "llm_weight_grams": grams,
                "parsed_signature": parsed_signature,
                "sample_signature": sample_signature,
            }
            for key in _lookup_name_variants(ingredient):
                index.setdefault(key, payload)
    return index


def _lookup_recipe1m_llm_weight_fallback(
    name: str,
    measurement: Any,
    qty: Optional[str],
    unit: Optional[str],
) -> Optional[dict]:
    index = _load_recipe1m_llm_weight_index()
    if not index:
        return None

    current_signature = _measurement_signature(
        measurement,
        parsed_qty=qty,
        parsed_unit=unit,
    )
    if current_signature is None:
        return None

    for key in _lookup_name_variants(name):
        row = index.get(key)
        if not row:
            continue
        if (
            current_signature == row.get("parsed_signature")
            or current_signature == row.get("sample_signature")
        ):
            return {
                "ingredient": row.get("ingredient"),
                "sample_measurement": row.get("sample_measurement"),
                "grams": float(row.get("llm_weight_grams")),
                "signature": current_signature,
            }
    return None


@lru_cache(maxsize=1)
def _load_recipe1m_llm_portion_fallback_index() -> dict:
    by_name_unit: dict[tuple[str, str], dict[str, Any]] = {}
    by_usda_unit: dict[tuple[str, str], dict[str, Any]] = {}
    rows = _csv_rows_from_path_or_pg(RECIPE1M_LLM_PORTION_FALLBACK_CSV_PATH, "food_weights_updated")
    if not rows:
        return {"by_name_unit": by_name_unit, "by_usda_unit": by_usda_unit}

    for row in rows:
            ingredient = str(row.get("ingredient") or "").strip()
            if not ingredient:
                continue
            grams_raw = str(row.get("weight_per_unit_grams") or "").strip()
            if not grams_raw:
                continue
            try:
                grams_per_unit = float(grams_raw)
            except ValueError:
                continue
            if grams_per_unit <= 0:
                continue

            unit_raw = str(row.get("extracted_unit") or "").strip()
            if not unit_raw:
                _, unit_raw, _ = _split_measurement(row.get("measurement"))
            unit_norm = _clean_unit(unit_raw)
            if not unit_norm:
                continue

            payload = {
                "ingredient": ingredient,
                "unit": unit_norm,
                "grams_per_unit": grams_per_unit,
                "sample_measurement": str(row.get("measurement") or "").strip(),
                "usda_id": str(row.get("usda_id") or "").strip() or None,
            }
            for key in _lookup_name_variants(ingredient):
                by_name_unit.setdefault((key, unit_norm), payload)
            usda_id = str(payload.get("usda_id") or "").strip()
            if usda_id:
                by_usda_unit.setdefault((usda_id, unit_norm), payload)

    return {"by_name_unit": by_name_unit, "by_usda_unit": by_usda_unit}


def _lookup_recipe1m_llm_portion_fallback(
    name: str,
    unit: Optional[str],
    usda_id: Optional[str] = None,
) -> Optional[dict]:
    unit_norm = _clean_unit(str(unit or ""))
    if not unit_norm:
        return None

    index = _load_recipe1m_llm_portion_fallback_index()
    usda_id_norm = str(usda_id or "").strip()
    if usda_id_norm:
        hit = index.get("by_usda_unit", {}).get((usda_id_norm, unit_norm))
        if hit:
            return hit

    for key in _lookup_name_variants(name):
        hit = index.get("by_name_unit", {}).get((key, unit_norm))
        if hit:
            return hit
    return None


def _live_llm_weight_fallback(
    name: str,
    qty: Optional[str],
    unit: Optional[str],
) -> tuple[Optional[float], Optional[str]]:
    qty_for_llm = qty
    if qty_for_llm is None or not str(qty_for_llm).strip():
        qty_for_llm = "1"

    unit_for_llm = unit
    if unit_for_llm is None or not str(unit_for_llm).strip():
        unit_for_llm = _infer_unit_from_name(name) or "piece"
    try:
        grams = ingredient_weight_llm_tool.invoke(
            {
                "ingredient": name,
                "parsed_quantity": qty_for_llm,
                "parsed_unit": unit_for_llm,
            }
        )
    except Exception as exc:
        return None, f"live_llm_error:{exc}"

    try:
        grams_f = float(grams)
    except (TypeError, ValueError):
        return None, f"live_llm_non_numeric:{grams!r}"
    if grams_f < 0:
        return None, f"live_llm_negative:{grams_f}"
    return grams_f, None


def _weight_name_usda_link(name: str, unit: Optional[str]) -> Optional[dict]:
    if not unit:
        return None
    try:
        matched = find_weight_match_by_name(name=name, unit=unit)
    except Exception:
        return None
    if not matched:
        return None
    usda_id = str(matched.get("usda_id") or "").strip()
    if not usda_id:
        return None
    food_name = str(matched.get("food_name") or "").strip() or None
    return {
        "usda_id": usda_id,
        "canonical_id": None,
        "canonical": food_name,
        "usda_food_label": food_name,
        "food_group_id": None,
        "food_group": None,
        "distance": None,
        "similarity": None,
        "match_source": "weight_name_fallback",
        "match_collection": "usda_weights_json",
    }


def _detail_source(detail: dict) -> str:
    if bool(detail.get("fda_fallback")):
        return "FDA"
    if bool(detail.get("live_llm_fallback")) or bool(detail.get("llm_fallback")):
        return "LLM fallback"
    match_type = str(detail.get("match_type") or "").lower()
    if "live_llm" in match_type or "llm" in match_type:
        return "LLM fallback"
    if "fda" in match_type:
        return "FDA"
    return "USDA portion tables"


def _detail_match(detail: dict) -> Optional[str]:
    canonical = str(detail.get("usda_match_canonical") or "").strip()
    if canonical:
        return canonical
    usda_id = str(detail.get("usda_id") or "").strip()
    if usda_id:
        return usda_id
    portion = detail.get("portion_match") or {}
    if isinstance(portion, dict):
        source_food = str(portion.get("source_food_name") or "").strip()
        if source_food:
            return source_food
    return None


def _compact_detail(detail: dict) -> dict:
    return {
        "name": detail.get("name"),
        "parsed_quantity": detail.get("parsed_quantity"),
        "parsed_unit": detail.get("parsed_unit"),
        "quantity_inferred": detail.get("quantity_inferred"),
        "unit_inferred": detail.get("unit_inferred"),
        "match": _detail_match(detail),
        "weight_grams": detail.get("weight_grams"),
        "source": _detail_source(detail),
        "error": detail.get("error"),
    }


@tool
def ingredient_weight_tool_usda(
    ingredient_names: Any,
    measurements: Any,
    return_details: bool = False,
    debug: bool = False,
) -> list[float] | dict:
    """
    Calculates estimated weights using USDA portion data when possible.
    """
    names_list = [str(v) for v in _as_list(ingredient_names)]
    measures_list = _as_list(measurements)

    weights: list[float] = []
    details: list[dict] = []
    for idx, name in enumerate(names_list):
        measurement = measures_list[idx] if idx < len(measures_list) else None
        qty, unit, qty_inferred = _split_measurement(measurement)
        unit_missing_from_measurement = unit is None
        unit_inferred = False
        if qty is not None and unit is None:
            inferred_unit = _infer_unit_from_name(name)
            if inferred_unit:
                unit = inferred_unit
                unit_inferred = True
        link = canonical_name_to_usda(name)
        usda_match_source = "direct" if link else None
        usda_match_similarity = None
        if not link:
            link = _embedding_usda_link(name, unit=unit)
            if link:
                usda_match_source = str(link.get("match_source") or "embedding")
                usda_match_similarity = link.get("similarity")
        if not link:
            link = _weight_name_usda_link(name, unit=unit)
            if link:
                usda_match_source = str(link.get("match_source") or "weight_name_fallback")
        usda_match_collection = None if not link else link.get("match_collection")
        usda_id = link.get("usda_id") if link else None
        food_group = _food_group_for_link(link, usda_id)
        herb_spice_hint = _has_herb_spice_hint(name)
        powder_hint = "powder" in str(name or "").strip().lower()
        error = None
        portion_match = None
        match_type = None
        llm_weight_fallback_attempted = False
        llm_weight_fallback_failed = False

        # Missing qty for seasoning-like ingredients: force "1 pinch" fallback.
        if qty is None and _is_pinch_default_candidate(name, food_group, herb_spice_hint):
            grams = HERB_MISSING_UNIT_FALLBACK_GRAMS
            qty = "1"
            unit = HERB_MISSING_UNIT_FALLBACK_UNIT
            qty_inferred = True
            unit_inferred = True
            portion_match = {
                "portion_desc": f"{HERB_MISSING_UNIT_FALLBACK_UNIT} (fallback)",
                "grams_per_unit": HERB_MISSING_UNIT_FALLBACK_GRAMS,
            }
            weights.append(float(grams))
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": unit_inferred,
                "usda_id": usda_id,
                "food_group": food_group,
                "usda_match_source": usda_match_source,
                "usda_match_similarity": usda_match_similarity,
                "usda_match_collection": usda_match_collection,
                "usda_match_canonical": None if not link else link.get("canonical"),
                "portion_match": portion_match,
                "match_type": "pinch_default_missing_quantity_fallback",
                "weight_grams": float(grams),
                "error": None,
            })
            continue

        # Herbs/spices fallback: if quantity exists but unit is missing, treat it as pinch.
        if (
            usda_id
            and qty is not None
            and unit_missing_from_measurement
            and (
                (food_group == HERB_SPICE_FOOD_GROUP and herb_spice_hint)
                or powder_hint
            )
        ):
            qty_value = _parse_quantity_value(qty)
            if qty_value is not None:
                grams = qty_value * HERB_MISSING_UNIT_FALLBACK_GRAMS
                portion_match = {
                    "portion_desc": f"{HERB_MISSING_UNIT_FALLBACK_UNIT} (fallback)",
                    "grams_per_unit": HERB_MISSING_UNIT_FALLBACK_GRAMS,
                }
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": usda_id,
                    "food_group": food_group,
                    "usda_match_source": usda_match_source,
                    "usda_match_similarity": usda_match_similarity,
                    "usda_match_collection": usda_match_collection,
                    "usda_match_canonical": None if not link else link.get("canonical"),
                    "portion_match": portion_match,
                    "match_type": "herb_pinch_fallback",
                    "weight_grams": float(grams),
                    "error": None,
                })
                continue

        # Curated fallback (FDA/LLM): when quantity exists but unit is missing,
        # reuse ingredient-level grams-per-reference-unit.
        if usda_id and qty is not None and unit_missing_from_measurement:
            qty_value = _parse_quantity_value(qty)
            llm_fallback = _lookup_llm_unit_grams(name, usda_id=str(usda_id))
            if qty_value is not None and llm_fallback is not None:
                grams_per_unit = float(llm_fallback["grams_per_unit"])
                grams = qty_value * grams_per_unit
                llm_unit = str(llm_fallback.get("unit") or "").strip()
                fallback_source = str(llm_fallback.get("source") or "llm").strip().lower()
                fallback_source_file = str(
                    llm_fallback.get("source_file")
                    or (
                        FDA_UNIT_GRAMS_CSV_PATH
                        if fallback_source == "fda"
                        else LLM_UNIT_GRAMS_CSV_PATH
                    )
                )
                if fallback_source == "fda":
                    fallback_match_type = FDA_UNIT_GRAMS_MATCH_TYPE
                    fallback_note = FDA_UNIT_GRAMS_NOTE
                    fallback_label = "FDA reference fallback"
                else:
                    fallback_match_type = LLM_UNIT_GRAMS_MATCH_TYPE
                    fallback_note = LLM_UNIT_GRAMS_NOTE
                    fallback_label = "LLM fallback"
                portion_desc = f"{llm_unit} ({fallback_label})" if llm_unit else f"{fallback_label} unit"
                portion_match = {
                    "portion_desc": portion_desc,
                    "grams_per_unit": grams_per_unit,
                }
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": usda_id,
                    "food_group": food_group,
                    "usda_match_source": usda_match_source,
                    "usda_match_similarity": usda_match_similarity,
                    "usda_match_collection": usda_match_collection,
                    "usda_match_canonical": None if not link else link.get("canonical"),
                    "portion_match": portion_match,
                    "match_type": fallback_match_type,
                    "weight_grams": float(grams),
                    "error": None,
                    "llm_fallback": fallback_source == "llm",
                    "fda_fallback": fallback_source == "fda",
                    "fallback": True,
                    "fallback_source": fallback_source,
                    "fallback_note": fallback_note,
                    "llm_fallback_note": fallback_note,
                    "llm_fallback_source_file": fallback_source_file,
                    "llm_fallback_ingredient": llm_fallback.get("ingredient"),
                    "llm_fallback_usda_id": llm_fallback.get("usda_id"),
                })
                continue

        # LLM fallback (Recipe1M unmatched set): when USDA match is still missing,
        # reuse the LLM-estimated grams for the same ingredient+measurement signature.
        if not usda_id:
            llm_weight_fallback_attempted = True
            llm_weight_fallback = _lookup_recipe1m_llm_weight_fallback(
                name=name,
                measurement=measurement,
                qty=qty,
                unit=unit,
            )
            if llm_weight_fallback is not None:
                grams = float(llm_weight_fallback["grams"])
                portion_match = {
                    "portion_desc": "LLM weight fallback",
                    "grams_per_unit": None,
                }
                weights.append(float(grams))
                details.append({
                    "name": name,
                    "measurement_raw": measurement,
                    "parsed_quantity": qty,
                    "parsed_unit": unit,
                    "quantity_inferred": qty_inferred,
                    "unit_inferred": unit_inferred,
                    "usda_id": None,
                    "food_group": None,
                    "usda_match_source": None,
                    "usda_match_similarity": None,
                    "usda_match_collection": None,
                    "usda_match_canonical": None,
                    "portion_match": portion_match,
                    "match_type": RECIPE1M_LLM_WEIGHT_MATCH_TYPE,
                    "weight_grams": float(grams),
                    "error": None,
                    "llm_fallback": True,
                    "llm_fallback_note": RECIPE1M_LLM_WEIGHT_NOTE,
                    "llm_fallback_source_file": str(RECIPE1M_LLM_WEIGHT_CSV_PATH),
                    "llm_fallback_ingredient": llm_weight_fallback.get("ingredient"),
                    "llm_fallback_sample_measurement": llm_weight_fallback.get("sample_measurement"),
                    "llm_fallback_signature": llm_weight_fallback.get("signature"),
                })
                continue
            llm_weight_fallback_failed = True

        if not usda_id or qty is None or unit is None:
            if not usda_id:
                error = "missing_usda_id"
            elif qty is None:
                error = "missing_quantity"
            else:
                error = "missing_unit"

            live_llm_grams = None
            live_llm_error = None
            if error in {"missing_unit", "missing_usda_id", "missing_quantity"}:
                live_llm_grams, live_llm_error = _live_llm_weight_fallback(
                    name=name,
                    qty=qty,
                    unit=unit,
                )
                if live_llm_grams is not None:
                    if error == "missing_unit":
                        live_reason = "missing_unit"
                    elif error == "missing_usda_id":
                        live_reason = "missing_usda_id"
                    else:
                        live_reason = "missing_quantity"
                    weights.append(float(live_llm_grams))
                    details.append({
                        "name": name,
                        "measurement_raw": measurement,
                        "parsed_quantity": qty,
                        "parsed_unit": unit,
                        "quantity_inferred": qty_inferred,
                        "unit_inferred": unit_inferred,
                        "usda_id": usda_id,
                        "food_group": food_group,
                        "usda_match_source": usda_match_source,
                        "usda_match_similarity": usda_match_similarity,
                        "usda_match_collection": usda_match_collection,
                        "usda_match_canonical": None if not link else link.get("canonical"),
                        "portion_match": {
                            "portion_desc": "live LLM fallback",
                            "grams_per_unit": None,
                        },
                        "match_type": (
                            "live_llm_missing_unit_fallback"
                            if live_reason == "missing_unit"
                            else (
                                "live_llm_missing_usda_fallback"
                                if live_reason == "missing_usda_id"
                                else "live_llm_missing_quantity_fallback"
                            )
                        ),
                        "weight_grams": float(live_llm_grams),
                        "error": None,
                        "live_llm_fallback": True,
                        "live_llm_reason": live_reason,
                    })
                    continue

            weights.append(0.0)
            details.append({
                "name": name,
                "measurement_raw": measurement,
                "parsed_quantity": qty,
                "parsed_unit": unit,
                "quantity_inferred": qty_inferred,
                "unit_inferred": unit_inferred,
                "usda_id": usda_id,
                "food_group": food_group,
                "usda_match_source": usda_match_source,
                "usda_match_similarity": usda_match_similarity,
                "usda_match_collection": usda_match_collection,
                "usda_match_canonical": None if not link else link.get("canonical"),
                "portion_match": None,
                "match_type": None,
                "weight_grams": None,
                "error": error,
                "llm_weight_fallback_attempted": llm_weight_fallback_attempted,
                "llm_weight_fallback_failed": llm_weight_fallback_failed,
                "llm_weight_fallback_error": (
                    "no_match_for_ingredient_measurement_signature"
                    if llm_weight_fallback_failed
                    else None
                ),
                "llm_fallback_note": (
                    RECIPE1M_LLM_WEIGHT_NOTE
                    if llm_weight_fallback_attempted
                    else None
                ),
                "llm_fallback_source_file": (
                    str(RECIPE1M_LLM_WEIGHT_CSV_PATH)
                    if llm_weight_fallback_attempted
                    else None
                ),
                "live_llm_fallback": False,
                "live_llm_error": live_llm_error,
            })
            continue

        grams = None
        try:
            grams, portion_match, match_type = _estimate_grams_from_usda_id(
                name=name,
                qty=qty,
                unit=unit,
                usda_id=str(usda_id),
            )
        except ValueError:
            # If direct canonical link exists but doesn't support this unit,
            # try embedding fallback to recover a better USDA id.
            if usda_match_source == "direct":
                emb_link = _embedding_usda_link(name, unit=unit)
                emb_usda_id = (
                    None if not emb_link else str(emb_link.get("usda_id") or "").strip()
                )
                if emb_usda_id and emb_usda_id != str(usda_id):
                    try:
                        grams, portion_match, match_type = _estimate_grams_from_usda_id(
                            name=name,
                            qty=qty,
                            unit=unit,
                            usda_id=emb_usda_id,
                        )
                        link = emb_link
                        usda_id = emb_usda_id
                        usda_match_source = "embedding"
                        usda_match_similarity = emb_link.get("similarity")
                        usda_match_collection = emb_link.get("match_collection")
                    except ValueError:
                        grams = None
            # Final fallback: estimate using the USDA weights index by name+unit.
            if grams is None:
                try:
                    grams, portion_match, name_match_usda_id = _estimate_grams_from_name_portion(
                        name=name,
                        qty=qty,
                        unit=unit,
                    )
                    match_type = "weight_name_portion_fallback"
                    if name_match_usda_id:
                        usda_id = name_match_usda_id
                        usda_match_source = "weight_name_fallback"
                        usda_match_similarity = None
                        usda_match_collection = "usda_weights_json"
                        link = usda_id_to_link(usda_id)
                        food_group = _food_group_for_link(link, usda_id)
                except ValueError:
                    grams = None
            if grams is None:
                llm_portion = _lookup_recipe1m_llm_portion_fallback(
                    name=name,
                    unit=unit,
                    usda_id=str(usda_id),
                )
                qty_value = _parse_quantity_value(qty)
                if llm_portion is not None and qty_value is not None:
                    grams_per_unit = float(llm_portion["grams_per_unit"])
                    grams = float(qty_value) * grams_per_unit
                    portion_match = {
                        "portion_desc": f"{_clean_unit(str(unit or ''))} (LLM per-unit fallback)",
                        "grams_per_unit": grams_per_unit,
                        "source_food_name": llm_portion.get("ingredient"),
                        "source": "llm_per_unit_fallback",
                        "sample_measurement": llm_portion.get("sample_measurement"),
                    }
                    match_type = RECIPE1M_LLM_PORTION_FALLBACK_MATCH_TYPE
            if grams is None:
                error = "missing_portion_for_unit"

        if grams is None and error == "missing_portion_for_unit":
            live_llm_grams, live_llm_error = _live_llm_weight_fallback(
                name=name,
                qty=qty,
                unit=unit,
            )
            if live_llm_grams is not None:
                grams = float(live_llm_grams)
                portion_match = {
                    "portion_desc": "live LLM fallback",
                    "grams_per_unit": None,
                }
                match_type = "live_llm_missing_portion_fallback"
                error = None
            else:
                # Keep original error and expose why live fallback failed.
                if live_llm_error:
                    error = f"missing_portion_for_unit ({live_llm_error})"

        weights.append(float(grams) if grams is not None else 0.0)
        details.append({
            "name": name,
            "measurement_raw": measurement,
            "parsed_quantity": qty,
            "parsed_unit": unit,
            "quantity_inferred": qty_inferred,
            "unit_inferred": unit_inferred,
            "usda_id": usda_id,
            "food_group": food_group,
            "usda_match_source": usda_match_source,
            "usda_match_similarity": usda_match_similarity,
            "usda_match_collection": usda_match_collection,
            "usda_match_canonical": None if not link else link.get("canonical"),
            "portion_match": portion_match,
            "match_type": match_type,
            "weight_grams": float(grams) if grams is not None else None,
            "error": error,
            "live_llm_fallback": bool(match_type and str(match_type).startswith("live_llm_")),
        })
    if return_details:
        if debug:
            return {"weights": weights, "details": details}
        return {"weights": weights, "details": [_compact_detail(d) for d in details]}
    return weights

    
def Ingredient_Weight_Node(state: RecipeState) -> RecipeState:
    debug = bool(state.debug)

    names = state.ingredient_names or []
    measurements = state.measurements or []
    result = ingredient_weight_tool_usda.invoke({
        "ingredient_names": names,
        "measurements": measurements,
        "return_details": True,
        "debug": debug,
    })
    if isinstance(result, dict):
        state.weights = result.get("weights", [])
    else:
        state.weights = result

    trace = dict(state.pipeline_trace or {})
    if isinstance(result, dict):
        weight_details = result.get("details", [])
        trace["weight_calculation"] = {
            "weights": result.get("weights", []),
            "details": weight_details,
            "matched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is not None
            ),
            "unmatched_count": sum(
                1 for item in weight_details if isinstance(item, dict) and item.get("weight_grams") is None
            ),
        }
    else:
        trace["weight_calculation"] = {"weights": result}
    state.pipeline_trace = trace

    if debug:
        print("\n[Ingredient_Weight_Node] Used USDA weights to estimate grams.")
        print("[Ingredient_Weight_Node] Updated State Keys:", list(state.model_dump().keys()))

    return state
