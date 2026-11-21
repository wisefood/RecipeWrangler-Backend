# Purpose: USDA weight/portion calculation helpers.

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional



DEFAULT_WEIGHTS = Path(":pg:usda-weights-v2")
DEFAULT_UNIT_VOLUMES = Path(":pg:unit_volume_ml_ground_truth")

AMBIGUOUS_PORTION_DESC_PATTERNS = (
    "dry yields",
    "amount to make",
    "guideline amount per",
    "prepared",
    "recipe",
    "yields",
    "yield",
)


def _load_data(path: str) -> list | dict:
    """Load from local file or Postgres depending on path sentinel."""
    if path.startswith(":pg:"):
        from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data

        try:
            return load_pipeline_data(path[4:])
        except Exception:
            return []
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


@lru_cache(maxsize=1)
def _weights_by_food(weights_path: str) -> dict[str, list[dict]]:
    data = _load_data(weights_path)
    return {str(row["usda_id"]): row.get("portions", []) for row in data if row.get("usda_id")}

@lru_cache(maxsize=1)
def _weights_by_name(weights_path: str) -> dict[str, list[dict]]:
    data = _load_data(weights_path)
    by_name: dict[str, list[dict]] = {}
    for row in data:
        if not row.get("usda_id"):
            continue
        name = row.get("food_name")
        if not name:
            continue
        key = str(name).strip().lower()
        if not key:
            continue
        by_name.setdefault(key, []).extend(row.get("portions", []))
    return by_name


@lru_cache(maxsize=1)
def _weight_rows(weights_path: str) -> list[dict]:
    return _load_data(weights_path)


@lru_cache(maxsize=1)
def _weight_food_names(weights_path: str) -> dict[str, str]:
    return {str(row['usda_id']): row.get('food_name', '')
            for row in _weight_rows(weights_path) if row.get('usda_id')}


def weight_food_name(usda_id: str, weights_path: Path = DEFAULT_WEIGHTS) -> Optional[str]:
    """Return the actual portion-table food label, not a canonical-link alias."""
    return _weight_food_names(str(weights_path)).get(str(usda_id))


@lru_cache(maxsize=1)
def _unit_volumes_ml(unit_volumes_path: str) -> dict[str, float]:
    path_str = str(unit_volumes_path)
    if path_str.startswith(":pg:"):
        payload = _load_data(path_str)
    elif not Path(unit_volumes_path).exists():
        return {}
    else:
        payload = json.loads(Path(unit_volumes_path).read_text(encoding="utf-8"))

    units_raw = payload.get("units_ml") if isinstance(payload, dict) else payload
    if not isinstance(units_raw, dict):
        return {}

    out: dict[str, float] = {}
    for raw_unit, raw_ml in units_raw.items():
        try:
            ml = float(raw_ml)
        except (TypeError, ValueError):
            continue
        if ml <= 0:
            continue
        unit_norm = _norm_unit(str(raw_unit))
        if not unit_norm:
            continue
        out[unit_norm] = ml
    return out


def _portions_for_food(
    usda_id: Optional[str],
    name: Optional[str],
    weights_path: str,
) -> list[dict]:
    portions = []
    if usda_id:
        portions = _weights_by_food(weights_path).get(str(usda_id), [])
    if not portions and name:
        portions = _weights_by_name(weights_path).get(str(name).strip().lower(), [])
    return portions


def _is_ambiguous_portion_desc(portion_desc: str) -> bool:
    desc = str(portion_desc or "").strip().lower()
    if any(pattern in desc for pattern in AMBIGUOUS_PORTION_DESC_PATTERNS):
        return True
    return bool(re.search(r"\byields?\b", desc))


def _normalized_portion(portion: dict) -> Optional[dict]:
    if _is_ambiguous_portion_desc(str(portion.get("portion_desc", ""))):
        return None
    try:
        grams = float(portion.get("grams"))
    except (TypeError, ValueError):
        return None
    if grams <= 0:
        return None

    grams_per_unit = None
    try:
        amount = float(portion.get("amount"))
    except (TypeError, ValueError):
        amount = None
    if amount is not None and amount > 0:
        grams_per_unit = grams / amount
    else:
        try:
            raw_grams_per_unit = float(portion.get("grams_per_unit"))
        except (TypeError, ValueError):
            raw_grams_per_unit = None
        if raw_grams_per_unit is not None and raw_grams_per_unit > 0:
            grams_per_unit = raw_grams_per_unit

    if grams_per_unit is None or grams_per_unit <= 0:
        return None

    normalized = dict(portion)
    normalized["grams_per_unit"] = float(grams_per_unit)
    return normalized


def find_weight_match_by_name(
    name: str,
    unit: str,
    weights_path: Path = DEFAULT_WEIGHTS,
    allow_unlinked: bool = False,
) -> Optional[dict]:
    key = str(name).strip().lower()
    if not key:
        return None

    unit_norm = _norm_unit(str(unit))
    if not unit_norm:
        return None

    rows = _weight_rows(str(weights_path))
    exact = []
    prefix = []
    token_matches = []
    for row in rows:
        if not allow_unlinked and not row.get("usda_id"):
            continue
        food_name = str(row.get("food_name", "")).strip()
        if not food_name:
            continue
        food_key = food_name.lower()
        if not portion_food_form_compatible(key, food_key):
            continue
        if food_key == key:
            exact.append(row)
        elif food_key.startswith(f"{key},") or food_key.startswith(f"{key} "):
            prefix.append(row)
        elif (_token_set(key) - {"fresh", "raw"}) and (_token_set(key) - {"fresh", "raw"}) <= _token_set(food_key):
            token_matches.append(row)

    for candidates in (exact, sorted(prefix, key=lambda r: len(str(r.get("food_name", "")))), sorted(token_matches, key=lambda r: len(str(r.get("food_name", ""))))):
        for row in candidates:
            food_name = str(row.get("food_name", "")).strip()
            unit_matches = [
                normalized
                for portion in row.get("portions", [])
                if (normalized := _normalized_portion(portion)) is not None
                and _portion_unit(str(normalized.get("portion_desc", ""))) == unit_norm
            ]
            best = _best_portion_for_name(unit_matches, key)
            if best:
                return {
                    "food_name": food_name,
                    "usda_id": str(row.get("usda_id")) if row.get("usda_id") is not None else None,
                    "portion": best,
                }
    return None


def _norm_unit(unit: str) -> str:
    unit = unit.strip().lower().strip(".,")
    aliases = {
        "tb": "tablespoon",
        "tbl": "tablespoon",
        "tbsp": "tablespoon",
        "tbsp.": "tablespoon",
        "tbsps": "tablespoon",
        "tablespoons": "tablespoon",
        "t": "teaspoon",
        "tsp": "teaspoon",
        "tsp.": "teaspoon",
        "teaspoons": "teaspoon",
        "ounces": "ounce",
        "ozs": "ounce",
        "oz": "ounce",
        "oz.": "ounce",
        "fl oz": "fluid ounce",
        "fluid ounces": "fluid ounce",
        "cups": "cup",
        "c": "cup",
        "c.": "cup",
        "lbs": "pound",
        "lb": "pound",
        "pounds": "pound",
    }
    return aliases.get(unit, unit)


def _portion_unit(portion_desc: str) -> str:
    cleaned = portion_desc.strip().lower()
    cleaned = cleaned.split("(")[0].strip()
    cleaned = re.sub(r"^[0-9./\s]+", "", cleaned).strip()
    if not cleaned:
        return ""

    # Preserve known multi-word units, otherwise reduce descriptors
    # like "cup elbows" -> "cup".
    if cleaned.startswith("fl oz"):
        return _norm_unit("fl oz")
    if cleaned.startswith("fluid ounce"):
        return _norm_unit("fluid ounce")
    if cleaned.startswith("fluid ounces"):
        return _norm_unit("fluid ounces")

    first = cleaned.split()[0]
    return _norm_unit(first)


def _normalize_token(token: str) -> str:
    irregular = {'leaves': 'leaf', 'halves': 'half', 'loaves': 'loaf',
                 'potatoes': 'potato', 'tomatoes': 'tomato', 'mangoes': 'mango'}
    if token in irregular:
        return irregular[token]
    if len(token) <= 3:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "xes", "zes", "sses")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _token_set(text: Optional[str]) -> set[str]:
    tokens = re.findall(r"[a-z]+", str(text or "").lower())
    return {_normalize_token(t) for t in tokens if t}


def portion_food_form_compatible(source: str, candidate: str) -> bool:
    """Do not widen a food name into a different preparation or derivative."""
    source_tokens = _token_set(source)
    candidate_tokens = _token_set(candidate)
    produce = {'carrot', 'zucchini', 'courgette', 'squash', 'cucumber', 'spinach',
               'lettuce', 'beetroot', 'potato', 'aubergine', 'eggplant', 'corn'}
    if source_tokens & produce and (('baby' in source_tokens) != ('baby' in candidate_tokens)):
        return False
    if 'celeriac' in source_tokens and not ('celeriac' in candidate_tokens or {'celery', 'root'} <= candidate_tokens):
        return False
    if source_tokens & {'turnip', 'beet', 'beetroot', 'radish', 'mustard'}:
        leaf_words = r'\b(?:greens|leaf|leaves)\b'
        if bool(re.search(leaf_words, source.lower())) != bool(re.search(leaf_words, candidate.lower())):
            return False
    if 'egg' in source_tokens:
        for part in ('white', 'yolk'):
            if (part in source_tokens) != (part in candidate_tokens):
                return False
    if {'coconut', 'water'} <= candidate_tokens and 'water' not in source_tokens:
        return False
    cooked = {"cooked", "boiled", "fried", "roasted", "baked"}
    staples = {'rice', 'pasta', 'lentil', 'bean', 'quinoa', 'oat', 'couscous', 'noodle', 'spaghetti',
               'macaroni', 'barley', 'chickpea', 'bulgur', 'egg'}
    stated_state = cooked | {'raw', 'dry', 'dried', 'uncooked', 'canned', 'steamed', 'instant'}
    # A recipe's "1 cup rice" is the dry staple unless a state is written.
    if source_tokens & staples and not source_tokens & stated_state and candidate_tokens & (cooked | {'steamed', 'instant'}):
        return False
    if 'gluten' in candidate_tokens and 'gluten' not in source_tokens:
        return False
    # Words that name a different food or variety must be written by the user.
    for token in ('wild', 'squash', 'babyfood', 'sheep', 'goat', 'buffalo', 'human',
                  'chocolate', 'buttermilk', 'evaporated', 'condensed', 'glutinous'):
        token = _normalize_token(token)
        if token in candidate_tokens and token not in source_tokens:
            if token == 'squash' and source_tokens & {'zucchini', 'courgette', 'pumpkin', 'butternut'}:
                continue  # USDA files these under "Squash"
            return False
    varieties = {'white', 'brown', 'black', 'red', 'wild'}
    if source_tokens & varieties and candidate_tokens & varieties and not source_tokens & candidate_tokens & varieties:
        return False
    if ((source_tokens & {"raw", "fresh"} and candidate_tokens & cooked)
            or (source_tokens & cooked and candidate_tokens & {"raw", "fresh"})):
        return False
    dry_staple = bool(candidate_tokens & {'lentil', 'soybean', 'chickpea', 'bean', 'rice', 'barley', 'oat'}
                      and 'raw' in candidate_tokens and not candidate_tokens & {'green', 'snap', 'sprout'})
    for markers in (
        {"dry", "dried", "dehydrated"}, {"frozen"}, {"canned"},
        {"ground", "minced"},
        {"smoked", "kippered"}, {"pickled"},
        {"juice"}, {"milk", "beverage"}, {"oil"}, {"syrup"},
        {"butter"}, {"flour"}, {"powder", "powdered"}, {"paste"},
        {"flake", "flaked"}, {"topping"}, {"sausage"}, {"cream"}, {"bean", "soybean"},
    ):
        if markers == {"dry", "dried", "dehydrated"} and dry_staple and source_tokens & markers and not candidate_tokens & markers:
            continue  # USDA "raw" pulses and grains are the dry, uncooked state
        if markers == {"dry", "dried", "dehydrated"} and source_tokens & (staples - {'egg'}) and not source_tokens & stated_state:
            continue  # unstated staple state means the dry product
        if bool(source_tokens & markers) != bool(candidate_tokens & markers):
            return False
    return True


def _best_portion_for_name(matches: list[dict], name: Optional[str]) -> Optional[dict]:
    # "medium wedge" is a sized piece, not a medium whole fruit.
    cut_parts = {"slice", "wedge", "chunk", "bite", "half"}
    named_parts = _token_set(name) & cut_parts
    matches = [p for p in matches
               if _portion_unit(str(p.get("portion_desc", ""))) not in {"small", "medium", "large"}
               or not (_token_set(str(p.get("portion_desc", ""))) & cut_parts) - named_parts]
    if not matches:
        return None
    if not name:
        return matches[0]
    name_tokens = _token_set(name)
    if not name_tokens:
        return matches[0]

    best = matches[0]
    best_score = (-1, -1)
    preparations = {"chopped", "ground", "sliced", "diced", "shredded", "whole", "halved"}
    requested = name_tokens & preparations
    for portion in matches:
        portion_tokens = _token_set(str(portion.get("portion_desc", "")))
        # Unspecified preparation should not prefer a transformed form over whole.
        unexpected = (portion_tokens & preparations) - requested - {"whole"}
        score = (len(portion_tokens & name_tokens), -len(unexpected))
        if score > best_score:
            best = portion
            best_score = score
    return best


def _volume_ml_for_unit(
    unit: str,
    unit_volumes_path: Path = DEFAULT_UNIT_VOLUMES,
) -> Optional[float]:
    unit_norm = _norm_unit(str(unit))
    if not unit_norm:
        return None
    # Metric volumes are definitions; the configurable table supplies kitchen units.
    metric = {"ml": 1.0, "milliliter": 1.0, "milliliters": 1.0,
              "millilitre": 1.0, "millilitres": 1.0,
              "dl": 100.0, "deciliter": 100.0, "decilitre": 100.0,
              "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
              "liters": 1000.0, "litres": 1000.0}
    if unit_norm in metric:
        return metric[unit_norm]
    return _unit_volumes_ml(str(unit_volumes_path)).get(unit_norm)


def _density_from_portions(
    portions: list[dict],
    name: Optional[str] = None,
    unit_volumes_path: Path = DEFAULT_UNIT_VOLUMES,
) -> Optional[dict]:
    candidates: list[dict] = []
    for portion in portions:
        normalized = _normalized_portion(portion)
        if normalized is None:
            continue
        portion_desc = str(normalized.get("portion_desc", ""))
        unit = _portion_unit(portion_desc)
        unit_ml = _volume_ml_for_unit(unit, unit_volumes_path=unit_volumes_path)
        if unit_ml is None:
            continue
        grams_per_unit = float(normalized["grams_per_unit"])
        candidates.append(
            {
                "portion_desc": portion_desc,
                "unit": unit,
                "unit_ml": unit_ml,
                "grams_per_unit": grams_per_unit,
                "density_g_per_ml": grams_per_unit / unit_ml,
            }
        )

    if not candidates:
        return None

    best_source = _best_portion_for_name(candidates, name)
    if best_source is None:
        return None

    return {
        "density_g_per_ml": float(best_source["density_g_per_ml"]),
        "candidate_count": len(candidates),
        "source_portion_desc": best_source.get("portion_desc"),
        "source_unit": best_source.get("unit"),
        "source_unit_ml": float(best_source.get("unit_ml")),
        "source_grams_per_unit": float(best_source.get("grams_per_unit")),
    }


def density_for_food(
    usda_id: Optional[str],
    name: Optional[str] = None,
    weights_path: Path = DEFAULT_WEIGHTS,
    unit_volumes_path: Path = DEFAULT_UNIT_VOLUMES,
) -> Optional[dict]:
    portions = _portions_for_food(usda_id, name, str(weights_path))
    if not portions:
        return None
    return _density_from_portions(
        portions,
        name=name,
        unit_volumes_path=unit_volumes_path,
    )

def _parse_quantity(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    range_match = re.match(r"^([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)$", text)
    if range_match:
        start = float(range_match.group(1))
        end = float(range_match.group(2))
        return (start + end) / 2.0
    if " " in text:
        whole, frac = text.split(" ", 1)
        return float(whole) + _parse_quantity(frac)
    if "/" in text:
        num, den = text.split("/", 1)
        den_f = float(den)
        if den_f == 0:
            raise ValueError("Invalid quantity value")
        return float(num) / den_f
    return float(text)

def _combine_measurement(quantity: object, unit: object) -> Optional[str]:
    qty = "" if quantity is None else str(quantity).strip()
    unit_s = "" if unit is None else str(unit).strip()
    combined = f"{qty} {unit_s}".strip()
    return combined or None

def _parse_measurement(measurement: object) -> tuple[Optional[str], Optional[str]]:
    if measurement is None:
        return None, None
    text = str(measurement).strip()
    if not text:
        return None, None
    match = re.match(r"^([0-9./\s-]+)\s*(.*)$", text)
    if not match:
        return None, None
    qty = match.group(1).strip() or None
    unit = match.group(2).strip() or None
    return qty, unit


def grams_for_food_id(
    usda_id: str,
    unit: str,
    value: float,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[float]:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id), [])
    if not portions:
        print(f"WARNING: no weights found for usda_id={usda_id}")
        return None

    unit_norm = _norm_unit(unit)
    matches = [
        normalized
        for p in portions
        if (normalized := _normalized_portion(p)) is not None
        and _portion_unit(str(normalized.get("portion_desc", ""))) == unit_norm
    ]
    if not matches:
        print(f"WARNING: no unit match for usda_id={usda_id} unit={unit}")
        return None
    first = matches[0]
    try:
        grams_per_unit_f = float(first.get("grams_per_unit"))
    except (TypeError, ValueError):
        return None
    try:
        qty = _parse_quantity(value)
    except (TypeError, ValueError):
        return None
    return grams_per_unit_f * qty


def weight_from_density_fallback(
    usda_id: Optional[str],
    unit: str,
    quantity: object,
    name: Optional[str] = None,
    weights_path: Path = DEFAULT_WEIGHTS,
    unit_volumes_path: Path = DEFAULT_UNIT_VOLUMES,
) -> Optional[dict]:
    target_unit_norm = _norm_unit(str(unit))
    target_unit_ml = _volume_ml_for_unit(target_unit_norm, unit_volumes_path=unit_volumes_path)
    if target_unit_ml is None:
        return None

    try:
        qty = _parse_quantity(quantity)
    except (TypeError, ValueError):
        return None

    density = density_for_food(
        usda_id=usda_id,
        name=name,
        weights_path=weights_path,
        unit_volumes_path=unit_volumes_path,
    )
    if density is None:
        return None

    grams_per_target_unit = density["density_g_per_ml"] * target_unit_ml
    grams = qty * grams_per_target_unit
    return {
        "grams": float(grams),
        "quantity": float(qty),
        "target_unit": target_unit_norm,
        "target_unit_ml": float(target_unit_ml),
        "grams_per_target_unit": float(grams_per_target_unit),
        "density_g_per_ml": float(density["density_g_per_ml"]),
        "density_candidate_count": int(density["candidate_count"]),
        "source_portion_desc": density.get("source_portion_desc"),
        "source_unit": density.get("source_unit"),
        "source_unit_ml": density.get("source_unit_ml"),
        "source_grams_per_unit": density.get("source_grams_per_unit"),
    }


def get_portions(
    usda_id: str,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> dict:
    portions = _weights_by_food(str(weights_path)).get(str(usda_id))
    if not portions:
        raise ValueError(f"No portions found for usda_id={usda_id}")
    return {"usda_id": str(usda_id), "portions": portions}


def match_portion(
    usda_id: str,
    unit: str,
    name: Optional[str] = None,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> Optional[dict]:
    portions = _portions_for_food(usda_id, name, str(weights_path))
    if not portions:
        return None
    unit_norm = _norm_unit(str(unit))
    matches = [
        normalized
        for p in portions
        if (normalized := _normalized_portion(p)) is not None
        and _portion_unit(str(normalized.get("portion_desc", ""))) == unit_norm
    ]
    return _best_portion_for_name(matches, name)


def weight_from_ingredient(
    ingredient: dict,
    weights_path: Path = DEFAULT_WEIGHTS,
) -> float:
    usda_id = ingredient.get("usda_id")
    unit = ingredient.get("unit")
    quantity = ingredient.get("quantity")
    name = ingredient.get("name")
    measurement = ingredient.get("measurement")

    if not measurement:
        measurement = _combine_measurement(quantity, unit)
        if measurement:
            ingredient["measurement"] = measurement

    if (unit is None or str(unit).strip() == "" or quantity is None) and measurement:
        parsed_qty, parsed_unit = _parse_measurement(measurement)
        if quantity is None and parsed_qty is not None:
            quantity = parsed_qty
        if (unit is None or str(unit).strip() == "") and parsed_unit is not None:
            unit = parsed_unit

    if not usda_id or unit is None or quantity is None:
        raise ValueError("Ingredient must include usda_id, unit, and quantity")

    # If the unit is already grams, return the provided quantity directly.
    unit_norm = _norm_unit(str(unit))
    if unit_norm in {"g", "gram", "grams"}:
        try:
            return _parse_quantity(quantity)
        except (TypeError, ValueError):
            raise ValueError("Invalid quantity value")

    portions = _portions_for_food(usda_id, name, str(weights_path))
    if not portions:
        raise ValueError(f"No portions found for usda_id={usda_id} name={name}")

    matches = [
        normalized
        for p in portions
        if (normalized := _normalized_portion(p)) is not None
        and _portion_unit(str(normalized.get("portion_desc", ""))) == unit_norm
    ]
    try:
        qty = _parse_quantity(quantity)
    except (TypeError, ValueError):
        raise ValueError("Invalid quantity value")

    best_match = _best_portion_for_name(matches, str(name) if name is not None else None)
    if best_match:
        try:
            grams_per_unit = float(best_match.get("grams_per_unit"))
        except (TypeError, ValueError):
            raise ValueError("Invalid grams_per_unit value")
        return qty * grams_per_unit

    density_result = weight_from_density_fallback(
        usda_id=str(usda_id),
        unit=str(unit),
        quantity=quantity,
        name=name,
        weights_path=weights_path,
    )
    if density_result is not None:
        return float(density_result["grams"])

    raise ValueError(f"No unit match found for usda_id={usda_id} name={name}")
