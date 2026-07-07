"""Convert ESSRG PLANEAT meals into unified, search-compatible recipes.

Each source meal becomes one recipe. Its component dishes are flattened into
the standard ingredient and instruction fields while also being retained in
``components`` for provenance. ESSRG directly assigns CoFID IDs to foods; no
ingredient matching is performed by this conversion.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

EXCEL = Path("PLANEAT T442 MEAL DB LL ESSRG.xlsx")
OUTPUT_DIR = Path("data/ESSRG")
OUTPUT_FILE = OUTPUT_DIR / "ESSRG_recipes_clean.json"
AUDIT_FILE = OUTPUT_DIR / "ESSRG_conversion_audit.json"

SOURCE = "ESSRG"
# The source contains meal quantities, but no explicit serving count.
DEFAULT_SERVES = 1.0
SEASONS = ("Autumn", "Winter", "Spring", "Summer")
NUTRIENT_COLUMNS = {
    "energy_kcal": ("1.3 Proximates", "Energy (kcal) (kcal)"),
    "protein_g": ("1.3 Proximates", "Protein (g)"),
    "carbohydrate_g": ("1.3 Proximates", "Carbohydrate (g)"),
    "fat_g": ("1.3 Proximates", "Fat (g)"),
    "sugar_g": ("1.3 Proximates", "Total sugars (g)"),
    "saturated_fat_g": ("1.3 Proximates", "Satd FA /100g fd (g)"),
    "sodium_mg": ("1.4 Inorganics", "Sodium (mg)"),
    "fibre_g": ("1.3 Proximates", "AOAC fibre (g)"),
}


def _text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _parse_measurement(value: Any) -> tuple[str | None, float | None, str | None]:
    """Preserve the source measurement and parse simple g/ml quantities."""
    measurement = _text(value)
    if not measurement:
        return None, None, None

    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(g|ml)?\s*", measurement, re.I)
    if not match:
        return measurement, None, None

    quantity = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "g").lower()
    return measurement, quantity, unit


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    return (
        pd.read_excel(EXCEL, sheet_name="Meals", header=0),
        pd.read_excel(EXCEL, sheet_name="Dishes", header=0),
    )


def _nutrient_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"", "N", "NAN"}:
            return None
        if normalized in {"TR", "TRACE"}:
            return 0.0
        value = normalized
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_cofid_lookup() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Read the embedded CoFID sheets and index nutrients by ID and exact name."""
    sheets: dict[str, pd.DataFrame] = {}
    for sheet_name, _ in set(NUTRIENT_COLUMNS.values()):
        sheets[sheet_name] = pd.read_excel(EXCEL, sheet_name=sheet_name, header=0)

    proximates = sheets["1.3 Proximates"]
    code_column = "Food Code"
    foods: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}

    for _, row in proximates.iterrows():
        food_id = _text(row.get(code_column))
        food_name = _text(row.get("Food Name"))
        if not food_id or not re.fullmatch(r"\d+-\d+", food_id):
            continue
        foods[food_id] = {"food_name": food_name, "nutrients_per_100": {}}
        if food_name:
            names[food_name] = food_id

    # The Inorganics sheet's first column contains the same food codes but has
    # a blank header in this workbook.
    sheet_code_columns = {
        "1.3 Proximates": "Food Code",
        "1.4 Inorganics": sheets["1.4 Inorganics"].columns[0],
    }
    for nutrient, (sheet_name, column) in NUTRIENT_COLUMNS.items():
        frame = sheets[sheet_name]
        code_col = sheet_code_columns[sheet_name]
        for _, row in frame.iterrows():
            food_id = _text(row.get(code_col))
            if food_id not in foods:
                continue
            foods[food_id]["nutrients_per_100"][nutrient] = _nutrient_float(
                row.get(column)
            )

    return foods, names


def build_dish_lookup(dishes_df: pd.DataFrame) -> dict[float, dict[str, Any]]:
    lookup: dict[float, dict[str, Any]] = {}

    for _, row in dishes_df.iterrows():
        dish_id = row.get("ID")
        dish_name = _text(row.get("Name"))
        if not isinstance(dish_id, (int, float)) or pd.isna(dish_id) or not dish_name:
            continue

        ingredients = []
        for index in range(1, 11):
            suffix = "" if index == 1 else f".{index - 1}"
            food_name = _text(row.get(f"Food #{index}"))
            if not food_name:
                continue

            cofid_id = _text(row.get(f"CoFID  ID{suffix}"))
            measurement, quantity, unit = _parse_measurement(
                row.get(f"Quantity (g/ml){suffix}")
            )
            ingredients.append(
                {
                    "name": food_name,
                    "measurement": measurement,
                    "quantity": quantity,
                    "unit": unit,
                    "weight_g": quantity if unit == "g" else None,
                    "composition_source": "cofid",
                    "composition_food_id": cofid_id,
                }
            )

        lookup[float(dish_id)] = {
            "dish_id": int(dish_id),
            "name": dish_name,
            "animal_product_category": _slug(
                _text(row.get("Contains animal products?"))
            ),
            "instructions": _text(row.get("Recipe [OPTIONAL]")),
            "ingredients": ingredients,
        }

    return lookup


def extract_components(
    meal_row: pd.Series, dish_lookup: dict[float, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    components = []
    missing_references = []

    for index in range(1, 11):
        # pandas names the duplicate meal ID columns ID.1 through ID.10.
        dish_id = meal_row.get(f"ID.{index}")
        source_dish_name = _text(meal_row.get(f"Dish #{index}"))
        if pd.isna(dish_id):
            continue

        try:
            numeric_id = float(dish_id)
        except (TypeError, ValueError):
            missing_references.append(
                {"dish_id": _text(dish_id), "dish_name": source_dish_name}
            )
            continue

        dish = dish_lookup.get(numeric_id)
        if dish is None:
            missing_references.append(
                {"dish_id": int(numeric_id), "dish_name": source_dish_name}
            )
            continue

        component = {
            **dish,
            "source_position": index,
            "source_name": source_dish_name,
        }
        component["ingredients"] = [
            {**ingredient, "component": dish["name"], "component_id": dish["dish_id"]}
            for ingredient in dish["ingredients"]
        ]
        components.append(component)

    return components, missing_references


def create_recipe(
    meal_id: float,
    meal_row: pd.Series,
    dish_lookup: dict[float, dict[str, Any]],
    cofid_lookup: dict[str, dict[str, Any]],
    cofid_name_lookup: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_id = str(int(meal_id))
    recipe_id = f"{SOURCE}_{source_id}"
    title = _text(meal_row.get("Name")) or recipe_id
    meal_type = _slug(_text(meal_row.get("Type")))
    animal_category = _slug(_text(meal_row.get("Contains animal products?")))
    seasonality = [
        season.lower()
        for season in SEASONS
        if str(meal_row.get(season, "")).strip().upper() == "Y"
    ]
    components, missing_references = extract_components(meal_row, dish_lookup)

    ingredient_details = [
        ingredient
        for component in components
        for ingredient in component["ingredients"]
    ]
    ingredients = [ingredient["name"] for ingredient in ingredient_details]

    instructions = [
        f"{component['name']}: {component['instructions']}"
        for component in components
        if component.get("instructions")
    ]
    nutrition = calculate_nutrition(
        ingredient_details, cofid_lookup, cofid_name_lookup, DEFAULT_SERVES
    )

    tags = ["source:essrg"]
    if meal_type:
        tags.append(f"type:{meal_type}")
    if animal_category:
        tags.append(f"animal_product:{animal_category}")
    tags.extend(f"season:{season}" for season in seasonality)

    recipe = {
        # Standard recipe/Elasticsearch-facing fields.
        "id": recipe_id,
        "recipe_id": recipe_id,
        "source": SOURCE,
        "source_id": source_id,
        "title": title,
        "description": _text(meal_row.get("Description [OPTIONAL]")),
        "url": None,
        "image_url": None,
        "ingredients": ingredients,
        "instructions": instructions,
        "tags": tags,
        "dish_types": [meal_type] if meal_type else [],
        "allergens": [],
        "duration": None,
        "duration_minutes": None,
        "serves": DEFAULT_SERVES,
        # ESSRG-specific source metadata.
        "meal_type": meal_type,
        "animal_product_category": animal_category,
        "seasonality": seasonality,
        "ingredient_details": ingredient_details,
        "components": components,
        "source_serves_provided": False,
        "nutrition": nutrition,
    }
    return recipe, missing_references


def calculate_nutrition(
    ingredients: list[dict[str, Any]],
    cofid_lookup: dict[str, dict[str, Any]],
    cofid_name_lookup: dict[str, str],
    serves: float,
) -> dict[str, Any]:
    """Calculate recipe totals solely from source quantities and embedded CoFID."""
    totals = {nutrient: 0.0 for nutrient in NUTRIENT_COLUMNS}
    nutrient_quantity = {nutrient: 0.0 for nutrient in NUTRIENT_COLUMNS}
    total_quantity = 0.0
    resolved_quantity = 0.0
    resolved_count = 0
    unresolved = []

    for ingredient in ingredients:
        quantity = ingredient.get("quantity")
        if quantity is None or quantity < 0:
            unresolved.append(
                {
                    "component": ingredient.get("component"),
                    "ingredient": ingredient.get("name"),
                    "composition_food_id": ingredient.get("composition_food_id"),
                    "reason": "missing_or_invalid_quantity",
                }
            )
            continue

        total_quantity += quantity
        food_id = ingredient.get("composition_food_id")
        resolution = "source_cofid_id"
        food = cofid_lookup.get(str(food_id)) if food_id else None

        # A few workbook cells have blank or malformed IDs. An exact food-name
        # lookup against the same embedded CoFID table is deterministic and is
        # recorded explicitly; no semantic/fuzzy matching is used.
        if food is None:
            fallback_id = cofid_name_lookup.get(ingredient.get("name", ""))
            food = cofid_lookup.get(fallback_id) if fallback_id else None
            if food is not None:
                food_id = fallback_id
                resolution = "exact_cofid_name_fallback"

        ingredient["nutrition_food_id"] = food_id if food is not None else None
        ingredient["nutrition_resolution"] = resolution if food is not None else "unresolved"

        if food is None:
            unresolved.append(
                {
                    "component": ingredient.get("component"),
                    "ingredient": ingredient.get("name"),
                    "composition_food_id": ingredient.get("composition_food_id"),
                    "reason": "cofid_food_not_resolved",
                }
            )
            continue

        resolved_count += 1
        resolved_quantity += quantity
        scale = quantity / 100.0
        for nutrient, value in food["nutrients_per_100"].items():
            if value is None:
                continue
            totals[nutrient] += value * scale
            nutrient_quantity[nutrient] += quantity

    rounded_totals = {
        key: round(value, 6) if nutrient_quantity[key] > 0 else None
        for key, value in totals.items()
    }
    per_serving = {
        f"{key}_per_serving": (
            round(value / serves, 6)
            if serves > 0 and nutrient_quantity[key] > 0
            else None
        )
        for key, value in totals.items()
    }
    nutrient_coverage = {
        key: round(
            (nutrient_quantity[key] / total_quantity * 100.0)
            if total_quantity > 0
            else 0.0,
            3,
        )
        for key in NUTRIENT_COLUMNS
    }

    return {
        **rounded_totals,
        **per_serving,
        "serves": serves,
        "nutrition_source": "cofid",
        "nutrition_source_file": EXCEL.name,
        "calculation_method": "sum(quantity / 100 * cofid_value_per_100)",
        "source_recipe_nutrition_provided": False,
        "derived_from_source_ingredient_assignments": True,
        "ingredient_count": len(ingredients),
        "resolved_ingredient_count": resolved_count,
        "ingredients_with_quantity_count": sum(
            ingredient.get("quantity") is not None for ingredient in ingredients
        ),
        "ingredient_resolution_percent": round(
            (resolved_count / len(ingredients) * 100.0) if ingredients else 0.0,
            3,
        ),
        "quantity_coverage_percent": round(
            (resolved_quantity / total_quantity * 100.0)
            if total_quantity > 0
            else 0.0,
            3,
        ),
        "nutrient_coverage_percent": nutrient_coverage,
        "unresolved_ingredients": unresolved,
        "quantity_basis_note": (
            "Source quantities are labelled g/ml. Values are scaled directly "
            "against the workbook's per-100 composition values."
        ),
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    meals_df, dishes_df = load_data()
    dish_lookup = build_dish_lookup(dishes_df)
    cofid_lookup, cofid_name_lookup = build_cofid_lookup()

    recipes = []
    audit: dict[str, Any] = {
        "source_file": str(EXCEL),
        "total_meals": 0,
        "valid_recipes": 0,
        "empty_ingredient_lists": 0,
        "recipes_without_instructions": 0,
        "missing_dish_references": [],
        "invalid_cofid_references": [],
        "conversion_errors": [],
        "converted_recipes": [],
    }

    for _, meal_row in meals_df.iterrows():
        meal_id = meal_row.get("ID")
        if not isinstance(meal_id, (int, float)) or pd.isna(meal_id):
            continue
        if not _text(meal_row.get("Name")):
            continue

        audit["total_meals"] += 1
        try:
            recipe, missing_references = create_recipe(
                float(meal_id),
                meal_row,
                dish_lookup,
                cofid_lookup,
                cofid_name_lookup,
            )
            recipes.append(recipe)
            audit["valid_recipes"] += 1

            if not recipe["ingredients"]:
                audit["empty_ingredient_lists"] += 1
            if not recipe["instructions"]:
                audit["recipes_without_instructions"] += 1
            if missing_references:
                audit["missing_dish_references"].append(
                    {
                        "recipe_id": recipe["recipe_id"],
                        "references": missing_references,
                    }
                )

            invalid_cofid = [
                {
                    "component": ingredient["component"],
                    "ingredient": ingredient["name"],
                    "composition_food_id": ingredient["composition_food_id"],
                }
                for ingredient in recipe["ingredient_details"]
                if not ingredient.get("composition_food_id")
                or not re.fullmatch(
                    r"\d+-\d+", str(ingredient["composition_food_id"])
                )
            ]
            if invalid_cofid:
                audit["invalid_cofid_references"].append(
                    {"recipe_id": recipe["recipe_id"], "ingredients": invalid_cofid}
                )

            audit["converted_recipes"].append(
                {
                    "meal_id": int(meal_id),
                    "recipe_id": recipe["recipe_id"],
                    "title": recipe["title"],
                    "component_count": len(recipe["components"]),
                    "ingredient_count": len(recipe["ingredients"]),
                    "instruction_count": len(recipe["instructions"]),
                    "nutrition_quantity_coverage_percent": recipe["nutrition"][
                        "quantity_coverage_percent"
                    ],
                    "known_weight_g": sum(
                        ingredient["weight_g"] or 0
                        for ingredient in recipe["ingredient_details"]
                    ),
                }
            )
        except Exception as exc:
            audit["conversion_errors"].append(
                {
                    "meal_id": int(meal_id),
                    "title": _text(meal_row.get("Name")),
                    "error": str(exc),
                }
            )

    OUTPUT_FILE.write_text(
        json.dumps(recipes, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    AUDIT_FILE.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    tag_counts: defaultdict[str, int] = defaultdict(int)
    for recipe in recipes:
        for tag in recipe["tags"]:
            tag_counts[tag] += 1

    print(f"Recipes written: {len(recipes)} -> {OUTPUT_FILE}")
    print(f"Components: {sum(len(r['components']) for r in recipes)}")
    print(f"Ingredients: {sum(len(r['ingredients']) for r in recipes)}")
    print(f"Instruction sections: {sum(len(r['instructions']) for r in recipes)}")
    print(f"Empty recipes: {audit['empty_ingredient_lists']}")
    print(f"Conversion errors: {len(audit['conversion_errors'])}")
    print(f"Audit: {AUDIT_FILE}")


if __name__ == "__main__":
    main()
